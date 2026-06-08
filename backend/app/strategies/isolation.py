"""IsolatedRunner — one strategy, one dedicated demo account.

This is the strategy-isolation test harness. Unlike StrategyRunner /
QuantRunner (which fan one bot's signals to many subscribers' accounts),
an IsolatedRunner binds a single bot to a single dedicated TradeLocker
demo sub-account (a ``StrategyAccount``) so its equity / drawdown / Sharpe
never cross-contaminate another strategy's.

Design choices (confirmed with the operator):
  - Provisioning: many sub-accounts under ONE Genesis FX login. The access
    /refresh tokens live on the owning ``User`` row and are refreshed by the
    existing token-refresh helper; this runner only pins WHICH sub-account
    (``tradelocker_account_id`` + ``tradelocker_acc_num``) it trades.
  - Isolation unit: per bot/strategy. The registry is keyed by ``bot_id``.
  - Staggered start: each runner sleeps ``start_offset_seconds`` + jitter
    before its first tick so N runners don't hammer the broker in lockstep.
  - clientOrderId carries an ``iso-`` audit tag identifying the account.

It deliberately reuses the leaf building blocks (TradeLockerClient,
BarFetcher, LaTPFNClient, the Strategy subclasses, PositionMonitor) but
keeps its own lean tick loop — no multi-user fan-out, no copy semantics —
so it stays decoupled from the fragile copy-runner.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import time
from datetime import datetime
from typing import Optional

from app.core.crypto import decrypt
from app.core.tradelocker_client import TradeLockerClient
from app.db.database import SessionLocal
from app.db.models import (
    Bot,
    Execution,
    ExecutionStatus,
    Signal,
    StrategyAccount,
    StrategyState,
    StrategyType,
    User,
)
from app.monitoring import slippage_tracker
from app.strategies.base import StrategySignal
from app.strategies.data_feed import BarFetcher
from app.strategies.latpfn_client import LaTPFNClient
from app.strategies.momentum import LatPFNMomentumStrategy
from app.strategies.position_monitor import PositionMonitor
from app.strategies.quant_strategy import LatPFNQuantStrategy
from app.strategies.runner import tf_seconds

logger = logging.getLogger(__name__)

# Registry keyed by bot_id — the isolation unit is per bot/strategy.
_ISO_RUNNERS: dict[int, "IsolatedRunner"] = {}

_SUPPORTED = (StrategyType.latpfn_momentum, StrategyType.latpfn_quant)


def get_iso_runner(bot_id: int) -> Optional["IsolatedRunner"]:
    return _ISO_RUNNERS.get(bot_id)


def list_iso_runners() -> dict[int, "IsolatedRunner"]:
    return dict(_ISO_RUNNERS)


class IsolatedRunner:
    def __init__(
        self,
        db_session_factory,
        strategy_account_id: int,
        latpfn_endpoint: Optional[str] = None,
    ) -> None:
        self.db_session_factory = db_session_factory
        self.strategy_account_id = strategy_account_id
        self.latpfn_endpoint = latpfn_endpoint or os.getenv(
            "LATPFN_ENDPOINT_URL"
        ) or None
        # Resolved on start() so the registry can key by bot_id.
        self.bot_id: int = -1
        self.timeframe: str = "1m"
        self.symbols: list[str] = []
        # Used by the slippage tracker. Set on start() to the bot's slug
        # (e.g. "velocity_spike") so audit rows are keyed by a human-readable
        # strategy name, not a numeric bot_id.
        self.strategy_name: str = ""
        self.task: Optional[asyncio.Task] = None
        self._stop = asyncio.Event()

    # ------------------------------------------------------------------ #
    # lifecycle
    # ------------------------------------------------------------------ #
    @classmethod
    async def start(
        cls,
        db_session_factory,
        strategy_account_id: int,
        latpfn_endpoint: Optional[str] = None,
    ) -> "IsolatedRunner":
        runner = cls(db_session_factory, strategy_account_id, latpfn_endpoint)

        # Resolve the binding up-front so we can key the registry by bot and
        # fail fast on a misconfigured account.
        db = db_session_factory()
        try:
            sa = db.get(StrategyAccount, strategy_account_id)
            if sa is None or not sa.is_active:
                raise ValueError(
                    f"strategy_account {strategy_account_id} not found/inactive"
                )
            bot = db.get(Bot, sa.bot_id)
            if bot is None or not getattr(bot, "is_active", True):
                raise ValueError(f"bot {sa.bot_id} not found/inactive")
            if bot.strategy_type not in _SUPPORTED:
                raise ValueError(
                    f"isolation harness supports {[s.value for s in _SUPPORTED]}; "
                    f"bot {bot.id} is {bot.strategy_type.value} "
                    "(webhook-driven bots run via /api/webhooks)"
                )
            runner.bot_id = sa.bot_id
            runner.timeframe = sa.timeframe or "1m"
            runner.strategy_name = bot.slug or f"bot-{sa.bot_id}"
            runner.symbols = [
                s.strip().upper()
                for s in (bot.instruments_csv or "").split(",")
                if s.strip()
            ]
            if not runner.symbols:
                raise ValueError(f"bot {bot.id} has no instruments configured")
        finally:
            db.close()

        existing = _ISO_RUNNERS.get(runner.bot_id)
        if existing and existing.task and not existing.task.done():
            return existing  # idempotent — one isolated runner per bot

        runner.task = asyncio.create_task(
            runner.run_loop(), name=f"iso-runner-bot{runner.bot_id}"
        )
        _ISO_RUNNERS[runner.bot_id] = runner
        return runner

    async def stop(self) -> None:
        self._stop.set()
        if self.task and not self.task.done():
            self.task.cancel()
            try:
                await self.task
            except (asyncio.CancelledError, Exception):
                pass
        _ISO_RUNNERS.pop(self.bot_id, None)
        self._mark_state(is_running=False)

    # ------------------------------------------------------------------ #
    # main loop
    # ------------------------------------------------------------------ #
    async def run_loop(self) -> None:
        db = self.db_session_factory()
        try:
            sa = db.get(StrategyAccount, self.strategy_account_id)
            bot = db.get(Bot, self.bot_id) if sa else None
            user = db.get(User, sa.user_id) if sa else None
            if sa is None or bot is None or user is None:
                logger.warning(
                    "iso runner bot=%s: binding/user missing, aborting",
                    self.bot_id,
                )
                return
            token = (
                decrypt(user.tradelocker_token)
                if user.tradelocker_token
                else None
            )
            if not token:
                logger.warning(
                    "iso runner bot=%s: owning user %s has no broker token",
                    self.bot_id,
                    user.id,
                )
                return
            account_id = sa.tradelocker_account_id
            acc_num = sa.tradelocker_acc_num or "1"
            env = sa.tradelocker_env or "demo"
            user_id = user.id
            start_offset = int(sa.start_offset_seconds or 0)
        finally:
            db.close()

        client = TradeLockerClient(env=env)
        latpfn_client = LaTPFNClient(endpoint_url=self.latpfn_endpoint)
        strategy = self._build_strategy(latpfn_client)

        async def _refresh_cb() -> Optional[str]:
            from app.core.tradelocker_token_refresh import refresh_user_token

            return await refresh_user_token(user_id)

        bar_fetcher = BarFetcher(
            client=client,
            account_id=account_id,
            token=token,
            acc_num=acc_num,
            token_refresh_cb=_refresh_cb,
        )
        position_monitor = PositionMonitor(
            self.db_session_factory,
            client,
            self.bot_id,
            self.timeframe,
            account_override=(account_id, acc_num),
            strategy_account_id=self.strategy_account_id,
        )

        self._init_state()

        # Staggered start: spread runners out so N of them don't all hit the
        # broker on the same wall-clock second. Deterministic offset from the
        # binding + small random jitter.
        delay = max(0, start_offset) + random.uniform(0, 5)
        if delay > 0:
            logger.info(
                "iso runner bot=%s staggered start in %.1fs", self.bot_id, delay
            )
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=delay)
                return  # stopped during the stagger window
            except asyncio.TimeoutError:
                pass

        sleep_seconds = tf_seconds(self.timeframe)
        logger.info(
            "iso runner started bot=%s acct=%s tf=%s symbols=%s",
            self.bot_id,
            account_id,
            self.timeframe,
            self.symbols,
        )

        while not self._stop.is_set():
            try:
                await self._tick(
                    strategy,
                    bar_fetcher,
                    client,
                    position_monitor,
                    account_id,
                    token,
                    acc_num,
                    user_id,
                )
            except Exception as exc:  # never let one tick kill the loop
                logger.exception("iso tick failed bot=%s: %s", self.bot_id, exc)
                self._mark_state(last_error=str(exc)[:500])

            try:
                await asyncio.wait_for(
                    self._stop.wait(), timeout=sleep_seconds
                )
            except asyncio.TimeoutError:
                pass

    def _build_strategy(self, latpfn_client: LaTPFNClient):
        db = self.db_session_factory()
        try:
            bot = db.get(Bot, self.bot_id)
            state = (
                db.query(StrategyState)
                .filter(
                    StrategyState.bot_id == self.bot_id,
                    StrategyState.timeframe == self.timeframe,
                )
                .first()
            )
            threshold = (
                float(state.confidence_threshold)
                if state and state.confidence_threshold
                else 0.8
            )
            is_quant = bot is not None and (
                bot.strategy_type == StrategyType.latpfn_quant
            )
        finally:
            db.close()

        common = dict(
            bot_id=self.bot_id,
            timeframe=self.timeframe,
            latpfn_client=latpfn_client,
            threshold=threshold,
        )
        if is_quant:
            return LatPFNQuantStrategy(**common)
        return LatPFNMomentumStrategy(**common)

    async def _tick(
        self,
        strategy,
        bar_fetcher: BarFetcher,
        client: TradeLockerClient,
        position_monitor: PositionMonitor,
        account_id: str,
        token: str,
        acc_num: str,
        user_id: int,
    ) -> None:
        for symbol in self.symbols:
            try:
                bars = await bar_fetcher.fetch_bars(
                    symbol, self.timeframe, count=240
                )
                # Refuse synthetic-fallback bars — a forecast on them looks
                # real but isn't actionable (mirrors the copy-runner guard).
                if bool(getattr(bars, "attrs", {}).get("synthetic", False)):
                    continue
                signal_obj = await strategy.on_bar(symbol, bars)
                # Capture bar_close_ts now — used as the audit reference
                # for every latency component downstream. Tolerate index
                # types other than DatetimeIndex by falling back to utcnow().
                try:
                    bar_close_ts = bars.index[-1].to_pydatetime().replace(tzinfo=None)
                except Exception:
                    bar_close_ts = datetime.utcnow()
            except Exception as exc:
                logger.warning(
                    "iso bot=%s symbol=%s failed: %s",
                    self.bot_id,
                    symbol,
                    exc,
                )
                continue

            if signal_obj is not None:
                try:
                    await self._fire(
                        signal_obj,
                        client,
                        position_monitor,
                        account_id,
                        token,
                        acc_num,
                        user_id,
                        bar_close_ts,
                        float(bars["close"].iloc[-1]),
                    )
                except Exception as exc:
                    logger.warning(
                        "iso fire bot=%s %s failed: %s",
                        self.bot_id,
                        symbol,
                        exc,
                    )

        try:
            await position_monitor.poll_and_close()
        except Exception as exc:
            logger.warning("iso position poll bot=%s: %s", self.bot_id, exc)

        self._mark_state(last_tick_at=datetime.utcnow(), last_error=None)

    async def _fire(
        self,
        sig: StrategySignal,
        client: TradeLockerClient,
        position_monitor: PositionMonitor,
        account_id: str,
        token: str,
        acc_num: str,
        user_id: int,
        bar_close_ts: datetime,
        bar_close_price: float,
    ) -> None:
        db = self.db_session_factory()
        try:
            payload = {
                "forecast_drift": sig.forecast_drift,
                "forecast_confidence": sig.forecast_confidence,
                "threshold": sig.threshold,
                "timeframe": self.timeframe,
                "isolated": True,
                "strategy_account_id": self.strategy_account_id,
                **sig.extra,
            }
            signal_row = Signal(
                bot_id=self.bot_id,
                instrument=sig.symbol,
                side=sig.side,
                entry_price=sig.entry_price,
                stop_loss=sig.stop_loss,
                take_profit=sig.take_profit,
                base_lot_size=sig.qty,
                raw_payload=json.dumps(payload),
            )
            db.add(signal_row)
            db.flush()
            signal_id = signal_row.id

            execution = Execution(
                signal_id=signal_id,
                user_id=user_id,
                status=ExecutionStatus.pending,
            )
            db.add(execution)
            db.flush()
            execution_id = execution.id
            db.commit()
        finally:
            db.close()

        # ---- Slippage tracker: phase 1 of 3 (signal emit) -------------
        # Best-effort: if the strategy populated the audit fields use them;
        # otherwise derive hard_stop_distance from the price/stop pair and
        # fall back to entry_price for expected_entry_price. Partner
        # strategies set the fields explicitly per the spec doc.
        expected_entry = (
            sig.expected_entry_price
            if sig.expected_entry_price
            else float(sig.entry_price)
        )
        hard_stop_dist = (
            sig.hard_stop_distance_pts
            if sig.hard_stop_distance_pts
            else abs(float(sig.entry_price) - float(sig.stop_loss))
        )
        try:
            slippage_record_id: Optional[int] = slippage_tracker.record_entry_signal(
                user_id=user_id,
                strategy_name=self.strategy_name,
                account_id=account_id,
                symbol=sig.symbol,
                side=sig.side,
                bar_close_ts=bar_close_ts,
                bar_close_price=bar_close_price,
                expected_entry_price=expected_entry,
                hard_stop_distance_pts=hard_stop_dist,
                trailing_stop_distance_pts=sig.trailing_stop_distance_pts,
                early_stop_condition=sig.early_stop_condition,
                execution_id=execution_id,
                extra=sig.extra,
            )
        except Exception as exc:  # never block the trade if tracker errors
            logger.warning(
                "iso bot=%s slippage_tracker.record_entry_signal failed: %s",
                self.bot_id,
                exc,
            )
            slippage_record_id = None

        # clientOrderId doubles as the audit tag (identifies the isolated
        # account) AND the idempotency key. Bucketed to 60s so a transient
        # retry within the same bar can't open a second position.
        bucket = int(time.time() // 60)
        client_order_id = (
            f"iso-{self.bot_id}-acct{self.strategy_account_id}-"
            f"{sig.symbol}-{sig.side}-{bucket}"
        )
        order_submit_ts = datetime.utcnow()
        order = await client.place_order(
            account_id=account_id,
            token=token,
            acc_num=acc_num,
            symbol=sig.symbol,
            side=sig.side,
            qty=sig.qty,
            sl=sig.stop_loss,
            tp=sig.take_profit,
            client_order_id=client_order_id,
        )
        order_ack_ts = datetime.utcnow()
        order_id = str(order.get("order_id") or "")

        # ---- Slippage tracker: phase 2 of 3 (fill confirmed) ---------
        # TradeLocker's place_order response doesn't include a fill price —
        # the broker assigns it server-side. Use sig.entry_price as our
        # best estimate (which is what the existing Execution row uses
        # anyway). When the reconciliation pipeline pulls the broker's
        # canonical fill record later, this can be corrected.
        if slippage_record_id is not None:
            try:
                slippage_tracker.record_fill(
                    slippage_record_id,
                    actual_entry_price=float(sig.entry_price),
                    order_submit_ts=order_submit_ts,
                    order_ack_ts=order_ack_ts,
                    fill_ts=order_ack_ts,
                    broker_response=order.get("raw") if isinstance(order, dict) else None,
                    execution_id=execution_id,
                )
            except Exception as exc:
                logger.warning(
                    "iso bot=%s slippage_tracker.record_fill failed: %s",
                    self.bot_id,
                    exc,
                )

        # Resolve the broker position id so PositionMonitor can detect close.
        # Compact version of the copy-runner's resolution: match by
        # orderId/strategyId, else newest position on this symbol+side.
        pos_id: Optional[str] = None
        try:
            positions = await client.get_positions(account_id, token, acc_num)
            for p in positions:
                p_order = str(p.get("orderId") or p.get("order_id") or "")
                p_strat = str(p.get("strategyId") or p.get("strategy_id") or "")
                if order_id and (p_order == order_id or p_strat == order_id):
                    pos_id = str(p.get("id"))
                    break
            if pos_id is None:
                same = [
                    p
                    for p in positions
                    if str(p.get("side", "")).lower() == sig.side.lower()
                ]
                try:
                    same.sort(
                        key=lambda p: p.get("openDate") or 0, reverse=True
                    )
                except Exception:
                    pass
                if same:
                    pos_id = str(same[0].get("id"))
        except Exception as exc:
            logger.warning(
                "iso bot=%s could not resolve position id: %s",
                self.bot_id,
                exc,
            )

        db = self.db_session_factory()
        try:
            ex = db.get(Execution, execution_id)
            if ex is not None:
                ex.status = ExecutionStatus.filled
                ex.tradelocker_order_id = pos_id or order_id or None
                ex.executed_lot_size = sig.qty
                ex.fill_price = sig.entry_price
                db.commit()
                # Pass slippage_record_id so PositionMonitor can finalize
                # the audit row when the position closes (phase 3 of 3).
                position_monitor.track(
                    ex,
                    sig.entry_price,
                    sig.side,
                    datetime.utcnow(),
                    slippage_record_id=slippage_record_id,
                    expected_exit_price=(
                        sig.entry_price + sig.trailing_stop_distance_pts
                        if sig.side.lower() == "buy"
                        else sig.entry_price - sig.trailing_stop_distance_pts
                    ),
                )
            self._mark_state(last_signal_at=datetime.utcnow())
        finally:
            db.close()

        logger.info(
            "iso bot=%s FIRED %s %s qty=%.4f coid=%s pos=%s",
            self.bot_id,
            sig.side,
            sig.symbol,
            sig.qty,
            client_order_id,
            pos_id,
        )

    # ------------------------------------------------------------------ #
    # StrategyState helpers (reuses the existing per-(bot,tf) status row so
    # the /strategy/status dashboard renders isolated runs too)
    # ------------------------------------------------------------------ #
    def _init_state(self) -> None:
        db = self.db_session_factory()
        try:
            state = (
                db.query(StrategyState)
                .filter(
                    StrategyState.bot_id == self.bot_id,
                    StrategyState.timeframe == self.timeframe,
                )
                .first()
            )
            now = datetime.utcnow()
            if state is None:
                state = StrategyState(
                    bot_id=self.bot_id,
                    timeframe=self.timeframe,
                    is_running=True,
                    started_at=now,
                )
                db.add(state)
            else:
                state.is_running = True
                state.started_at = now
                state.last_error = None
            db.commit()
        except Exception as exc:  # pragma: no cover — defensive
            logger.debug("iso _init_state failed: %s", exc)
        finally:
            db.close()

    def _mark_state(self, **fields) -> None:
        db = self.db_session_factory()
        try:
            state = (
                db.query(StrategyState)
                .filter(
                    StrategyState.bot_id == self.bot_id,
                    StrategyState.timeframe == self.timeframe,
                )
                .first()
            )
            if state is None:
                return
            for k, v in fields.items():
                setattr(state, k, v)
            db.commit()
        except Exception as exc:  # pragma: no cover — defensive
            logger.debug("iso _mark_state failed: %s", exc)
        finally:
            db.close()
