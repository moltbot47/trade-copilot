"""StrategyRunner — orchestrates one bot+timeframe across many symbols/users.

Per-tick:
  1. Sleep until the next bar close.
  2. For each symbol → fetch bars → call strategy.on_bar.
  3. If signal: persist Signal, fan out to subscribed users (place_order),
     persist Executions, hand each to the PositionMonitor.
  4. Poll PositionMonitor → close any finished trades into TradeOutcomes.
  5. Every N closed trades → recompute stats, run feedback loop.

The loop is hardened: any single iteration error is caught, logged to
StrategyState.last_error, and the loop continues.
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime
from typing import Any, Optional

from sqlalchemy.orm import Session

from app.core.crypto import decrypt
from app.core.risk_engine import compute_user_lot
from app.core.tradelocker_client import TradeLockerClient, TradeLockerError
from app.db.models import (
    Execution,
    ExecutionStatus,
    Signal,
    StrategyState,
    User,
)
from app.strategies.base import StrategySignal
from app.strategies.data_feed import BarFetcher
from app.strategies.feedback import FeedbackAdjuster
from app.strategies.latpfn_client import LaTPFNClient
from app.strategies.momentum import LatPFNMomentumStrategy
from app.strategies.performance_tracker import PerformanceTracker
from app.strategies.position_monitor import PositionMonitor
from app.strategies.quant_strategy import LatPFNQuantStrategy
from app.strategies.trade_manager import CohortCommand, TradeManager

logger = logging.getLogger(__name__)


def _ws_publish(channel: str, user_id: int, payload: dict) -> None:
    """Best-effort WS event-bus publish; never raise back into runner.

    Wave 3A's event_bus may not yet be wired up. Failure here is silent
    so that strategy execution is never blocked by the WS layer.
    """
    try:
        from app.ws.event_bus import event_bus
    except Exception:
        return
    try:
        result = event_bus.publish(channel, user_id, payload)
        if asyncio.iscoroutine(result):
            asyncio.ensure_future(result)
    except Exception as exc:  # pragma: no cover
        logger.debug("ws publish failed channel=%s: %s", channel, exc)


def tf_seconds(tf: str) -> int:
    if tf.endswith("m"):
        return int(tf[:-1]) * 60
    if tf.endswith("h"):
        return int(tf[:-1]) * 3600
    return 60


# Track running runner tasks so the API can stop them.
_RUNNERS: dict[tuple[int, str], "StrategyRunner"] = {}


def get_runner(bot_id: int, timeframe: str) -> Optional["StrategyRunner"]:
    return _RUNNERS.get((bot_id, timeframe))


class StrategyRunner:
    def __init__(
        self,
        db_session_factory,
        bot_id: int,
        timeframe: str,
        symbols: list[str],
        user_emails: list[str],
        latpfn_endpoint: Optional[str] = None,
        feedback_every_n: int = 20,
    ) -> None:
        self.db_session_factory = db_session_factory
        self.bot_id = bot_id
        self.timeframe = timeframe
        self.symbols = list(symbols)
        self.user_emails = list(user_emails)
        self.latpfn_endpoint = latpfn_endpoint
        self.feedback_every_n = feedback_every_n
        self.task: Optional[asyncio.Task] = None
        self._stop = asyncio.Event()
        self._last_feedback_count = 0

    @classmethod
    async def start(cls, **kwargs) -> "StrategyRunner":
        runner = cls(**kwargs)
        key = (runner.bot_id, runner.timeframe)
        existing = _RUNNERS.get(key)
        if existing and existing.task and not existing.task.done():
            return existing
        runner.task = asyncio.create_task(runner.run_loop(), name=f"runner-{runner.bot_id}-{runner.timeframe}")
        _RUNNERS[key] = runner
        return runner

    async def stop(self) -> None:
        self._stop.set()
        if self.task and not self.task.done():
            self.task.cancel()
            try:
                await self.task
            except (asyncio.CancelledError, Exception):
                pass
        _RUNNERS.pop((self.bot_id, self.timeframe), None)

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
            if state is not None:
                state.is_running = False
                db.commit()
        finally:
            db.close()

    async def run_loop(self) -> None:
        # Init: ensure StrategyState exists, mark running
        await self._init_state()

        client = TradeLockerClient(env="demo")
        latpfn_client = LaTPFNClient(endpoint_url=self.latpfn_endpoint)
        threshold = self._current_threshold()
        strategy = LatPFNMomentumStrategy(
            bot_id=self.bot_id,
            timeframe=self.timeframe,
            latpfn_client=latpfn_client,
            threshold=threshold,
        )
        position_monitor = PositionMonitor(self.db_session_factory, client, self.bot_id, self.timeframe)

        # The first user provides the data-feed credentials. (For shared bot
        # data we just need any authenticated user's session.)
        feed_user = self._load_first_authed_user()
        if feed_user is None:
            logger.warning("runner bot=%s tf=%s: no authenticated user for data feed", self.bot_id, self.timeframe)
            return
        bar_fetcher = BarFetcher(
            client=client,
            account_id=feed_user["account_id"],
            token=feed_user["token"],
            acc_num=feed_user["acc_num"],
        )

        sleep_seconds = tf_seconds(self.timeframe)
        logger.info(
            "runner started bot=%s tf=%s symbols=%s threshold=%.2f",
            self.bot_id,
            self.timeframe,
            self.symbols,
            threshold,
        )

        while not self._stop.is_set():
            try:
                # Refresh threshold from DB in case feedback adjusted it
                strategy.threshold = self._current_threshold()
                await self._tick(strategy, bar_fetcher, client, position_monitor)
            except Exception as exc:
                logger.exception("runner tick failed: %s", exc)
                self._record_error(str(exc)[:500])

            try:
                await asyncio.wait_for(self._stop.wait(), timeout=sleep_seconds)
            except asyncio.TimeoutError:
                pass  # next tick

    async def _tick(
        self,
        strategy: LatPFNMomentumStrategy,
        bar_fetcher: BarFetcher,
        client: TradeLockerClient,
        position_monitor: PositionMonitor,
    ) -> None:
        # Honor pause window
        if self._is_paused():
            return

        for symbol in self.symbols:
            try:
                bars = await bar_fetcher.fetch_bars(symbol, self.timeframe, count=240)
                signal_obj = await strategy.on_bar(symbol, bars)
            except Exception as exc:
                logger.warning("symbol %s failed: %s", symbol, exc)
                continue

            if signal_obj is not None:
                try:
                    await self._fire(signal_obj, client, position_monitor)
                except Exception as exc:
                    logger.warning("fire signal %s failed: %s", symbol, exc)

        # Always poll positions for closes
        try:
            outcomes = await position_monitor.poll_and_close()
        except Exception as exc:
            logger.warning("position poll failed: %s", exc)
            outcomes = []

        # Update last_tick_at
        self._touch_tick(len(outcomes))

        # Push a strategy snapshot to subscribed users on every tick. Cheap
        # query (state row + last 20 outcomes); skipped silently on error.
        try:
            self._publish_strategy_snapshot()
        except Exception as exc:  # pragma: no cover — defensive
            logger.debug("strategy snapshot publish failed: %s", exc)

        # Feedback on every Nth close
        if outcomes:
            db = self.db_session_factory()
            try:
                tracker = PerformanceTracker(db, self.bot_id)
                total = tracker.total_outcomes()
                if total - self._last_feedback_count >= self.feedback_every_n or self._last_feedback_count == 0:
                    stats = tracker.compute_rolling_stats(window=self.feedback_every_n)
                    cur = self._current_threshold()
                    new_t, action = await FeedbackAdjuster(db, self.bot_id).adjust(
                        stats, cur, timeframe=self.timeframe
                    )
                    tracker.write_snapshot(stats, new_t, action)
                    self._last_feedback_count = total
                    logger.info(
                        "feedback bot=%s tf=%s action=%s threshold=%.2f→%.2f",
                        self.bot_id,
                        self.timeframe,
                        action,
                        cur,
                        new_t,
                    )
            finally:
                db.close()

    async def _fire(
        self,
        sig: StrategySignal,
        client: TradeLockerClient,
        position_monitor: PositionMonitor,
    ) -> None:
        db = self.db_session_factory()
        try:
            payload = {
                "forecast_drift": sig.forecast_drift,
                "forecast_confidence": sig.forecast_confidence,
                "threshold": sig.threshold,
                "timeframe": self.timeframe,
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
            db.flush()  # get id

            users = self._load_users(db)
            # Emit per-user "signals" event for every subscriber regardless
            # of whether they have TL creds (so paper-mode users get fed too).
            signal_event = {
                "bot_id": self.bot_id,
                "symbol": sig.symbol,
                "side": sig.side,
                "confidence": sig.forecast_confidence,
                "drift": sig.forecast_drift,
                "threshold": sig.threshold,
                "entry_price": sig.entry_price,
                "stop_loss": sig.stop_loss,
                "take_profit": sig.take_profit,
                "timeframe": self.timeframe,
            }
            for u in users:
                _ws_publish("signals", u.id, signal_event)

            for user in users:
                token = decrypt(user.tradelocker_token) if user.tradelocker_token else None
                if not token or not user.tradelocker_account_id:
                    continue
                try:
                    state: Any = await client.get_account_state(
                        user.tradelocker_account_id, token, user.tradelocker_acc_num or "1"
                    )
                    balance = float(state.get("balance") or 10_000.0)
                except TradeLockerError as exc:
                    logger.info("balance lookup failed user=%s: %s", user.id, exc)
                    balance = 10_000.0

                # find user's subscription aggression for this bot
                aggression = 5
                for sub in user.subscriptions:
                    if sub.bot_id == self.bot_id and not sub.is_paused:
                        aggression = sub.aggression_level
                        break

                lot = compute_user_lot(
                    base_lot=sig.qty,
                    aggression_level=aggression,
                    account_balance=balance,
                    max_risk_pct=user.max_daily_loss_pct,
                )

                ex = Execution(
                    signal_id=signal_row.id,
                    user_id=user.id,
                    status=ExecutionStatus.pending,
                    executed_lot_size=lot,
                )
                db.add(ex)
                db.flush()

                try:
                    order = await client.place_order(
                        account_id=user.tradelocker_account_id,
                        token=token,
                        acc_num=user.tradelocker_acc_num or "1",
                        symbol=sig.symbol,
                        side=sig.side,
                        qty=lot,
                        sl=sig.stop_loss,
                        tp=sig.take_profit,
                    )
                    ex.tradelocker_order_id = str(order.get("order_id") or "")
                    ex.status = ExecutionStatus.filled
                    ex.fill_price = sig.entry_price
                    db.commit()
                    position_monitor.track(
                        ex,
                        entry_price=sig.entry_price,
                        side=sig.side,
                        opened_at=datetime.utcnow(),
                    )
                except TradeLockerError as exc:
                    ex.status = ExecutionStatus.error
                    ex.error_message = str(exc)[:500]
                    db.commit()
                    logger.warning("order failed user=%s: %s", user.id, exc)

            # mark last_signal_at
            state_row = (
                db.query(StrategyState)
                .filter(
                    StrategyState.bot_id == self.bot_id,
                    StrategyState.timeframe == self.timeframe,
                )
                .first()
            )
            if state_row is not None:
                state_row.last_signal_at = datetime.utcnow()
                db.commit()
        finally:
            db.close()

    # ---------- helpers ----------

    async def _init_state(self) -> None:
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
                state = StrategyState(
                    bot_id=self.bot_id,
                    timeframe=self.timeframe,
                    is_running=True,
                    confidence_threshold=1.5,
                    started_at=datetime.utcnow(),
                )
                db.add(state)
            else:
                state.is_running = True
                state.started_at = datetime.utcnow()
                state.last_error = None
                state.paused_until = None
            db.commit()
        finally:
            db.close()

    def _current_threshold(self) -> float:
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
            return float(state.confidence_threshold) if state else 1.5
        finally:
            db.close()

    def _is_paused(self) -> bool:
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
            if state and state.paused_until and state.paused_until > datetime.utcnow():
                return True
            return False
        finally:
            db.close()

    def _safe_state_update(self, mutator: callable, label: str) -> None:
        """Run a state-row mutator inside a short-lived session — never crash."""
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
            if state is not None:
                mutator(state)
                db.commit()
        except Exception as exc:  # noqa: BLE001
            try:
                db.rollback()
            except Exception:
                pass
            logger.warning("runner state update %s failed (continuing): %s", label, exc)
        finally:
            try:
                db.close()
            except Exception:
                pass

    def _record_error(self, msg: str) -> None:
        self._safe_state_update(
            lambda s: setattr(s, "last_error", msg),
            "last_error",
        )

    def _touch_tick(self, _outcome_count: int) -> None:
        self._safe_state_update(
            lambda s: setattr(s, "last_tick_at", datetime.utcnow()),
            "last_tick_at",
        )

    def _load_users(self, db: Session) -> list[User]:
        if not self.user_emails:
            return []
        return db.query(User).filter(User.email.in_(self.user_emails), User.is_active.is_(True)).all()

    def _publish_strategy_snapshot(self) -> None:
        """Build and publish a `strategy:{tf}` event to each subscribed user.

        Sends {state, performance, recent_trades} per WS_PROTOCOL.md.
        Best-effort — silently skips on any error.
        """
        if not self.user_emails:
            return
        from app.db.models import PerformanceSnapshot, TradeOutcome

        channel = f"strategy:{self.timeframe}"
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
            state_payload: dict[str, Any] = (
                {
                    "bot_id": state.bot_id,
                    "timeframe": state.timeframe,
                    "is_running": state.is_running,
                    "confidence_threshold": state.confidence_threshold,
                    "max_concurrent": state.max_concurrent,
                    "paused_until": state.paused_until.isoformat() if state.paused_until else None,
                    "last_tick_at": state.last_tick_at.isoformat() if state.last_tick_at else None,
                    "last_signal_at": state.last_signal_at.isoformat() if state.last_signal_at else None,
                    "last_error": state.last_error,
                    "started_at": state.started_at.isoformat() if state.started_at else None,
                }
                if state is not None
                else {}
            )

            perf_row = (
                db.query(PerformanceSnapshot)
                .filter(PerformanceSnapshot.bot_id == self.bot_id)
                .order_by(PerformanceSnapshot.snapshot_at.desc())
                .first()
            )
            perf_payload = (
                {
                    "win_rate": perf_row.win_rate,
                    "profit_factor": perf_row.profit_factor,
                    "sharpe": perf_row.sharpe,
                    "avg_r": perf_row.avg_r,
                    "max_drawdown_pct": perf_row.max_drawdown_pct,
                    "total_pnl_usd": perf_row.total_pnl_usd,
                    "total_trades": perf_row.total_trades,
                    "snapshot_at": perf_row.snapshot_at.isoformat() if perf_row.snapshot_at else None,
                }
                if perf_row is not None
                else None
            )

            recent_rows = (
                db.query(TradeOutcome)
                .filter(
                    TradeOutcome.bot_id == self.bot_id,
                    TradeOutcome.timeframe == self.timeframe,
                )
                .order_by(TradeOutcome.closed_at.desc())
                .limit(20)
                .all()
            )
            recent = [
                {
                    "id": r.id,
                    "instrument": r.instrument,
                    "side": r.side,
                    "entry_price": r.entry_price,
                    "exit_price": r.exit_price,
                    "qty": r.qty,
                    "pnl_usd": r.pnl_usd,
                    "r_multiple": r.r_multiple,
                    "closed_at": r.closed_at.isoformat() if r.closed_at else None,
                }
                for r in recent_rows
            ]

            users = self._load_users(db)
        finally:
            db.close()

        snapshot = {
            "state": state_payload,
            "performance": perf_payload,
            "recent_trades": recent,
        }
        for u in users:
            _ws_publish(channel, u.id, snapshot)

    def _load_first_authed_user(self) -> Optional[dict]:
        db = self.db_session_factory()
        try:
            users = db.query(User).filter(User.email.in_(self.user_emails), User.is_active.is_(True)).all()
            for u in users:
                token = decrypt(u.tradelocker_token) if u.tradelocker_token else None
                if token and u.tradelocker_account_id:
                    return {
                        "account_id": u.tradelocker_account_id,
                        "token": token,
                        "acc_num": u.tradelocker_acc_num or "1",
                    }
            return None
        finally:
            db.close()


# ----------------------------------------------------------------------------
# QuantRunner — pyramiding/scale-out/trail-SL strategy on top of LaT-PFN
# ----------------------------------------------------------------------------


class QuantRunner:
    """Runner for `LatPFNQuantStrategy`.

    Layered on top of the existing infrastructure (BarFetcher, LaTPFNClient,
    TradeLockerClient). Each tick:

      1. For each (user, symbol) with an open cohort:
         - fetch bars + forecast → call TradeManager.evaluate()
         - execute the returned CohortCommand (scale_in / partial_close /
           modify_sl / exit_all) via TradeLockerClient.

      2. For each (user, symbol) WITHOUT an open cohort:
         - call strategy.on_bar() to look for an entry.
         - if signal: open cohort + place initial leg.

    Cohort state lives in DB so it survives restarts. The runner is
    re-entrant; calling QuantRunner.start() twice for the same (bot, tf)
    returns the existing instance.
    """

    def __init__(
        self,
        db_session_factory,
        bot_id: int,
        timeframe: str,
        symbols: list[str],
        user_emails: list[str],
        latpfn_endpoint: Optional[str] = None,
    ) -> None:
        self.db_session_factory = db_session_factory
        self.bot_id = bot_id
        self.timeframe = timeframe
        self.symbols = list(symbols)
        self.user_emails = list(user_emails)
        self.latpfn_endpoint = latpfn_endpoint
        self.task: Optional[asyncio.Task] = None
        self._stop = asyncio.Event()

    @classmethod
    async def start(cls, **kwargs) -> "QuantRunner":
        runner = cls(**kwargs)
        key = (runner.bot_id, runner.timeframe)
        existing = _RUNNERS.get(key)
        if existing and existing.task and not existing.task.done():
            return existing  # type: ignore[return-value]
        runner.task = asyncio.create_task(
            runner.run_loop(), name=f"quant-{runner.bot_id}-{runner.timeframe}"
        )
        _RUNNERS[key] = runner  # type: ignore[assignment]
        return runner

    async def stop(self) -> None:
        self._stop.set()
        if self.task and not self.task.done():
            self.task.cancel()
            try:
                await self.task
            except (asyncio.CancelledError, Exception):
                pass
        _RUNNERS.pop((self.bot_id, self.timeframe), None)

        # Mark StrategyState.is_running=False so the dashboard reflects it.
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
            if state is not None:
                state.is_running = False
                db.commit()
        finally:
            db.close()

    async def run_loop(self) -> None:
        # Init: ensure StrategyState exists, mark running
        await self._init_state()

        client = TradeLockerClient(env="demo")
        latpfn_client = LaTPFNClient(endpoint_url=self.latpfn_endpoint)
        strategy = LatPFNQuantStrategy(
            bot_id=self.bot_id,
            timeframe=self.timeframe,
            latpfn_client=latpfn_client,
            threshold=1.5,
        )

        feed_user = self._load_first_authed_user()
        if feed_user is None:
            logger.warning("quant runner bot=%s tf=%s: no authed user", self.bot_id, self.timeframe)
            self._record_error("no authenticated user for data feed")
            return
        bar_fetcher = BarFetcher(
            client=client,
            account_id=feed_user["account_id"],
            token=feed_user["token"],
            acc_num=feed_user["acc_num"],
        )

        sleep_seconds = tf_seconds(self.timeframe)
        logger.info("quant runner started bot=%s tf=%s", self.bot_id, self.timeframe)

        while not self._stop.is_set():
            try:
                await self._tick(strategy, bar_fetcher, client)
                self._touch_tick()
            except Exception as exc:
                logger.exception("quant runner tick failed: %s", exc)
                self._record_error(str(exc)[:500])
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=sleep_seconds)
            except asyncio.TimeoutError:
                pass

    async def _tick(
        self,
        strategy: LatPFNQuantStrategy,
        bar_fetcher: BarFetcher,
        client: TradeLockerClient,
    ) -> None:
        for symbol in self.symbols:
            try:
                bars = await bar_fetcher.fetch_bars(symbol, self.timeframe, count=240)
            except Exception as exc:
                logger.warning("bars fetch failed %s: %s", symbol, exc)
                continue
            current_price = float(bars["close"].iloc[-1]) if len(bars) else None
            forecast_view = await strategy.forecast_view(bars) if current_price else None

            users = self._load_users()
            for user in users:
                token = decrypt(user.tradelocker_token) if user.tradelocker_token else None
                if not token or not user.tradelocker_account_id:
                    continue
                tm_db = self.db_session_factory()
                try:
                    tm = TradeManager(tm_db, self.bot_id, user.id, self.timeframe)
                    open_cohorts = tm.list_open_cohorts(symbol)
                    # 1) Manage open cohorts
                    for cohort in open_cohorts:
                        cmd = tm.evaluate(
                            cohort,
                            current_price=current_price or cohort.weighted_avg_entry,
                            forecast_drift=(forecast_view or {}).get("drift", 0.0),
                            forecast_confidence=(forecast_view or {}).get("confidence", 0.0),
                        )
                        if cmd is None:
                            continue
                        try:
                            await self._execute_command(tm, cohort, cmd, user, token, client, current_price or cohort.weighted_avg_entry)
                            tm_db.commit()
                        except Exception as exc:
                            tm_db.rollback()
                            logger.warning("cmd %s on cohort %s failed: %s", cmd.kind, cohort.id, exc)
                    # 2) Look for new entries (only if no open cohort for this symbol)
                    if not open_cohorts:
                        sig = await strategy.on_bar(symbol, bars)
                        if sig is not None and sig.extra.get("kind") == "entry":
                            try:
                                await self._open_new_cohort(tm, sig, user, token, client)
                                tm_db.commit()
                            except Exception as exc:
                                tm_db.rollback()
                                logger.warning("open cohort failed: %s", exc)
                finally:
                    tm_db.close()

    # ---------- command execution ----------

    async def _open_new_cohort(
        self,
        tm: TradeManager,
        sig: StrategySignal,
        user: User,
        token: str,
        client: TradeLockerClient,
    ) -> None:
        atr = float(sig.extra.get("atr", 0.0))
        order = await client.place_order(
            account_id=user.tradelocker_account_id,
            token=token,
            acc_num=user.tradelocker_acc_num or "1",
            symbol=sig.symbol,
            side=sig.side,
            qty=sig.qty,
            sl=sig.stop_loss,
            tp=sig.take_profit,
        )
        order_id = str(order.get("order_id") or "")
        # Find the position id
        positions = await client.get_positions(
            user.tradelocker_account_id, token, user.tradelocker_acc_num or "1"
        )
        # Newest position with matching symbol/side
        pos_id = None
        for p in reversed(positions):
            if str(p.get("side")).lower() == sig.side.lower():
                pos_id = str(p.get("id"))
                break
        cohort = tm.open_cohort(
            instrument=sig.symbol,
            side=sig.side,
            entry_price=sig.entry_price,
            atr=atr,
            qty=sig.qty,
            stop_loss=sig.stop_loss,
            take_profit=sig.take_profit,
            tl_position_id=pos_id,
            tl_order_id=order_id,
            forecast_drift=sig.forecast_drift,
            forecast_confidence=sig.forecast_confidence,
        )
        logger.info(
            "cohort %s opened %s %s qty=%.4f entry=%.2f", cohort.id, sig.side, sig.symbol, sig.qty, sig.entry_price
        )
        # Reflect signal time on StrategyState + push WS event.
        self._touch_signal()
        signal_event = {
            "bot_id": self.bot_id,
            "symbol": sig.symbol,
            "side": sig.side,
            "confidence": sig.forecast_confidence,
            "drift": sig.forecast_drift,
            "threshold": sig.threshold,
            "entry_price": sig.entry_price,
            "stop_loss": sig.stop_loss,
            "take_profit": sig.take_profit,
            "timeframe": self.timeframe,
            "kind": "entry",
            "cohort_id": cohort.id,
        }
        _ws_publish("signals", user.id, signal_event)

    async def _execute_command(
        self,
        tm: TradeManager,
        cohort: "Cohort",  # noqa
        cmd: CohortCommand,
        user: User,
        token: str,
        client: TradeLockerClient,
        current_price: float,
    ) -> None:
        from app.db.models import Cohort  # avoid circular at import time

        assert isinstance(cohort, Cohort)
        if cmd.kind == "scale_in":
            order = await client.place_order(
                account_id=user.tradelocker_account_id,
                token=token,
                acc_num=user.tradelocker_acc_num or "1",
                symbol=cohort.instrument,
                side=cohort.side,
                qty=cmd.qty,
                sl=cmd.new_stop,
                tp=cohort.initial_take_profit,
            )
            order_id = str(order.get("order_id") or "")
            positions = await client.get_positions(
                user.tradelocker_account_id, token, user.tradelocker_acc_num or "1"
            )
            pos_id = None
            for p in reversed(positions):
                if str(p.get("side")).lower() == cohort.side.lower():
                    pos_id = str(p.get("id"))
                    break
            tm.add_scale_in_leg(
                cohort,
                entry_price=current_price,
                qty=cmd.qty,
                stop_loss=cmd.new_stop,
                tl_position_id=pos_id,
                tl_order_id=order_id,
            )
            # Move existing entry-leg SL to break-even on the original
            try:
                for leg in cohort.legs:
                    if leg.role == "entry" and leg.is_open and leg.tradelocker_position_id:
                        await client.modify_position(
                            leg.tradelocker_position_id,
                            token=token,
                            acc_num=user.tradelocker_acc_num or "1",
                            stop_loss=cohort.weighted_avg_entry,
                        )
                        leg.stop_loss = float(cohort.weighted_avg_entry)
            except Exception as exc:
                logger.warning("entry-leg SL move failed: %s", exc)

        elif cmd.kind == "partial_close":
            close_resp = await client.partial_close(
                account_id=user.tradelocker_account_id,
                token=token,
                acc_num=user.tradelocker_acc_num or "1",
                position_id=cohort.legs[0].tradelocker_position_id or "",
                symbol=cohort.instrument,
                original_side=cohort.side,
                qty=cmd.qty,
            )
            tm.record_partial_close(
                cohort,
                qty_closed=cmd.qty,
                close_price=current_price,
                tl_order_id=str(close_resp.get("order_id") or ""),
            )
            # Move SL on remaining legs to break-even
            if cmd.new_stop is not None:
                tm.update_stop(cohort, cmd.new_stop)
                for leg in cohort.legs:
                    if leg.is_open and leg.role in ("entry", "scale_in") and leg.tradelocker_position_id:
                        try:
                            await client.modify_position(
                                leg.tradelocker_position_id,
                                token=token,
                                acc_num=user.tradelocker_acc_num or "1",
                                stop_loss=cmd.new_stop,
                            )
                        except Exception as exc:
                            logger.warning("SL move failed leg=%s: %s", leg.id, exc)

        elif cmd.kind == "modify_sl":
            if cmd.new_stop is None:
                return
            tm.update_stop(cohort, cmd.new_stop)
            for leg in cohort.legs:
                if leg.is_open and leg.role in ("entry", "scale_in") and leg.tradelocker_position_id:
                    try:
                        await client.modify_position(
                            leg.tradelocker_position_id,
                            token=token,
                            acc_num=user.tradelocker_acc_num or "1",
                            stop_loss=cmd.new_stop,
                        )
                    except Exception as exc:
                        logger.warning("trail SL failed leg=%s: %s", leg.id, exc)

        elif cmd.kind == "exit_all":
            for leg in cohort.legs:
                if leg.is_open and leg.tradelocker_position_id:
                    try:
                        await client.close_position(
                            leg.tradelocker_position_id,
                            token=token,
                            acc_num=user.tradelocker_acc_num or "1",
                        )
                    except Exception as exc:
                        logger.warning("close leg %s failed: %s", leg.id, exc)
            tm.close_cohort(cohort, current_price, reason=cmd.reason or "exit_all")

        # Any executed cohort command counts as a signal-level action.
        self._touch_signal()

    # ---------- helpers ----------

    async def _init_state(self) -> None:
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
                state = StrategyState(
                    bot_id=self.bot_id,
                    timeframe=self.timeframe,
                    is_running=True,
                    confidence_threshold=1.5,
                    started_at=datetime.utcnow(),
                )
                db.add(state)
            else:
                state.is_running = True
                state.started_at = datetime.utcnow()
                state.last_error = None
                state.paused_until = None
            db.commit()
        finally:
            db.close()

    def _safe_state_update(self, mutator: callable, label: str) -> None:
        """Run a state-row mutator inside its own short-lived session.

        Swallows DB errors (lock, transient connection issue) and logs at
        WARNING — a single status-update failure must NEVER kill the
        runner's asyncio task. The strategy keeps trading; the dashboard
        just misses one tick of the indicator.
        """
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
            if state is not None:
                mutator(state)
                db.commit()
        except Exception as exc:  # noqa: BLE001 — best effort
            try:
                db.rollback()
            except Exception:
                pass
            logger.warning("runner state update %s failed (continuing): %s", label, exc)
        finally:
            try:
                db.close()
            except Exception:
                pass

    def _touch_tick(self) -> None:
        self._safe_state_update(
            lambda s: setattr(s, "last_tick_at", datetime.utcnow()),
            "last_tick_at",
        )

    def _touch_signal(self) -> None:
        self._safe_state_update(
            lambda s: setattr(s, "last_signal_at", datetime.utcnow()),
            "last_signal_at",
        )

    def _record_error(self, msg: str) -> None:
        self._safe_state_update(
            lambda s: setattr(s, "last_error", msg),
            "last_error",
        )

    def _load_users(self) -> list[User]:
        if not self.user_emails:
            return []
        db = self.db_session_factory()
        try:
            return (
                db.query(User)
                .filter(User.email.in_(self.user_emails), User.is_active.is_(True))
                .all()
            )
        finally:
            db.close()

    def _load_first_authed_user(self) -> Optional[dict]:
        db = self.db_session_factory()
        try:
            users = (
                db.query(User)
                .filter(User.email.in_(self.user_emails), User.is_active.is_(True))
                .all()
            )
            for u in users:
                token = decrypt(u.tradelocker_token) if u.tradelocker_token else None
                if token and u.tradelocker_account_id:
                    return {
                        "account_id": u.tradelocker_account_id,
                        "token": token,
                        "acc_num": u.tradelocker_acc_num or "1",
                    }
            return None
        finally:
            db.close()
