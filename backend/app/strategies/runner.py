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
from typing import Any, Callable, Optional

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
from app.strategies.exhaustion_filter import passes_exhaustion, passes_no_chase
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


# Track running runner tasks so the API can stop them. Keyed by
# (bot_id, timeframe, runner_type_name) so StrategyRunner and QuantRunner
# can't accidentally overwrite each other on the same (bot, tf) — that
# bug previously caused type-confused returns where one class's instance
# leaked through a hole expected to hold the other class. Audit finding
# 2026-05-10 (HIGH severity).
_RUNNERS: dict[tuple[int, str, str], "StrategyRunner"] = {}


def _runner_key(bot_id: int, timeframe: str, runner_cls_name: str) -> tuple[int, str, str]:
    return (bot_id, timeframe, runner_cls_name)


def get_runner(bot_id: int, timeframe: str) -> Optional["StrategyRunner"]:
    """Return whichever runner type is registered for this (bot, tf).

    The API doesn't always know whether it's a StrategyRunner or QuantRunner
    upfront, so we look for either. If both happen to be running (which
    shouldn't happen but the registry now permits), QuantRunner wins.
    """
    for cls_name in ("QuantRunner", "StrategyRunner"):
        r = _RUNNERS.get((bot_id, timeframe, cls_name))
        if r is not None:
            return r
    return None


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
        key = _runner_key(runner.bot_id, runner.timeframe, cls.__name__)
        existing = _RUNNERS.get(key)
        if existing and existing.task and not existing.task.done() and isinstance(existing, cls):
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
        _RUNNERS.pop(_runner_key(self.bot_id, self.timeframe, type(self).__name__), None)

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

        # The first user provides the data-feed credentials AND determines
        # which TradeLocker env (demo/live) the client speaks to.
        feed_user = self._load_first_authed_user()
        if feed_user is None:
            logger.warning("runner bot=%s tf=%s: no authenticated user for data feed", self.bot_id, self.timeframe)
            return

        client = TradeLockerClient(env=feed_user.get("env") or "demo")
        latpfn_client = LaTPFNClient(endpoint_url=self.latpfn_endpoint)
        threshold = self._current_threshold()
        strategy = LatPFNMomentumStrategy(
            bot_id=self.bot_id,
            timeframe=self.timeframe,
            latpfn_client=latpfn_client,
            threshold=threshold,
        )
        position_monitor = PositionMonitor(self.db_session_factory, client, self.bot_id, self.timeframe)
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

    def _safe_state_update(self, mutator: Callable[[Any], None], label: str) -> None:
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
                        "user_id": u.id,
                        "account_id": u.tradelocker_account_id,
                        "token": token,
                        "acc_num": u.tradelocker_acc_num or "1",
                        "env": u.tradelocker_env or "demo",
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
        key = _runner_key(runner.bot_id, runner.timeframe, cls.__name__)
        existing = _RUNNERS.get(key)
        if existing and existing.task and not existing.task.done() and isinstance(existing, cls):
            return existing
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
        _RUNNERS.pop(_runner_key(self.bot_id, self.timeframe, type(self).__name__), None)

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

        feed_user = self._load_first_authed_user()
        if feed_user is None:
            logger.warning("quant runner bot=%s tf=%s: no authed user", self.bot_id, self.timeframe)
            self._record_error("no authenticated user for data feed")
            return

        # Pre-emptively refresh the feed user's token at runner startup.
        # If the bot has been idle for >24h, the stored token is almost
        # certainly stale and the first tick would 401 on every call.
        from app.core.tradelocker_token_refresh import refresh_user_token

        try:
            new_token = await refresh_user_token(feed_user["user_id"])
            if new_token:
                feed_user["token"] = new_token
                logger.info("quant runner: refreshed feed user %s token at startup", feed_user["user_id"])
        except Exception as exc:
            logger.warning("startup token refresh failed: %s", exc)

        # Build a token-refresh callback so the BarFetcher can self-heal
        # when a 401 hits mid-run (e.g. token rotated to a new value by
        # the periodic refresh task). Returns None on failure.
        feed_user_id = feed_user["user_id"]

        async def _bar_token_refresh() -> str | None:
            try:
                return await refresh_user_token(feed_user_id)
            except Exception as exc:
                logger.warning("bar-fetcher token refresh failed: %s", exc)
                return None

        client = TradeLockerClient(env=feed_user.get("env") or "demo")
        latpfn_client = LaTPFNClient(endpoint_url=self.latpfn_endpoint)

        # Honor whatever threshold is currently in StrategyState — set by
        # the seed (0.5), per-run override on /strategy/start, or feedback
        # adjuster.
        threshold = self._read_threshold(default=0.5)
        logger.info(
            "quant runner bot=%s tf=%s env=%s using threshold=%.2f",
            self.bot_id,
            self.timeframe,
            feed_user.get("env"),
            threshold,
        )
        strategy = LatPFNQuantStrategy(
            bot_id=self.bot_id,
            timeframe=self.timeframe,
            latpfn_client=latpfn_client,
            threshold=threshold,
        )
        bar_fetcher = BarFetcher(
            client=client,
            account_id=feed_user["account_id"],
            token=feed_user["token"],
            acc_num=feed_user["acc_num"],
            token_refresh_cb=_bar_token_refresh,
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

    def _log_tick(
        self,
        symbol: str,
        decision: str,
        current_price: float | None = None,
        forecast_view: dict | None = None,
        threshold: float | None = None,
        reason: str | None = None,
        user_id: int | None = None,
        extra: dict | None = None,
    ) -> None:
        """Persist a per-tick decision to StrategyTickLog + publish WS event.

        Best-effort: failure here must never break the trading loop.
        """
        try:
            from app.db.models import StrategyTickLog
            import json as _json

            db = self.db_session_factory()
            try:
                row = StrategyTickLog(
                    bot_id=self.bot_id,
                    user_id=user_id,
                    timeframe=self.timeframe,
                    symbol=symbol,
                    current_price=current_price,
                    forecast_drift=(forecast_view or {}).get("drift"),
                    forecast_std=(forecast_view or {}).get("std"),
                    forecast_confidence=(forecast_view or {}).get("confidence"),
                    threshold=threshold,
                    decision=decision,
                    reason=reason,
                    extra=_json.dumps(extra or {}),
                )
                db.add(row)
                db.commit()
                db.refresh(row)
            finally:
                db.close()

            # Publish to subscribed WS clients
            try:
                import asyncio as _asyncio
                from app.ws.event_bus import event_bus

                payload = {
                    "id": row.id,
                    "tick_at": row.tick_at.isoformat(),
                    "bot_id": row.bot_id,
                    "timeframe": row.timeframe,
                    "symbol": symbol,
                    "current_price": current_price,
                    "forecast_drift": (forecast_view or {}).get("drift"),
                    "forecast_std": (forecast_view or {}).get("std"),
                    "forecast_confidence": (forecast_view or {}).get("confidence"),
                    "threshold": threshold,
                    "decision": decision,
                    "reason": reason,
                }
                if user_id is not None:
                    result = event_bus.publish("analysis", user_id, payload)
                else:
                    result = event_bus.publish_global("analysis", payload)
                if _asyncio.iscoroutine(result):
                    _asyncio.ensure_future(result)
            except Exception:
                pass

            # Discord webhook fan-out (best-effort, allowlisted decisions only).
            try:
                from app.integrations.discord_signals import (
                    post_decision_fire_and_forget,
                )

                post_decision_fire_and_forget(
                    decision=decision,
                    bot_id=self.bot_id,
                    timeframe=self.timeframe,
                    symbol=symbol,
                    user_id=user_id,
                    current_price=current_price,
                    forecast_drift=(forecast_view or {}).get("drift"),
                    forecast_confidence=(forecast_view or {}).get("confidence"),
                    threshold=threshold,
                    reason=reason,
                    extra=extra,
                )
            except Exception:
                pass
        except Exception as exc:
            logger.debug("tick log failed: %s", exc)

    def _check_circuit_breaker(self, user: User) -> tuple[bool, str | None]:
        """Block entries if the user's consecutive-loss circuit breaker is tripped.

        Returns (allowed, reason). The breaker trips when the last N closed
        TradeOutcomes for this user across this bot are all losses (pnl < 0).
        Once tripped, ``circuit_breaker_tripped_at`` is set; entries are
        blocked until cooldown elapses. The runner clears the tripped state
        opportunistically on the next entry attempt past the cooldown.
        """
        from datetime import datetime, timedelta
        from app.db.models import TradeOutcome

        n_losses = int(user.circuit_breaker_n_losses or 0)
        if n_losses <= 0:
            return True, None

        # Check existing cooldown window
        tripped_at = user.circuit_breaker_tripped_at
        cooldown = int(user.circuit_breaker_cooldown_minutes or 60)
        if tripped_at is not None:
            elapsed = datetime.utcnow() - tripped_at
            if elapsed < timedelta(minutes=cooldown):
                remaining = timedelta(minutes=cooldown) - elapsed
                return False, f"circuit_breaker_cooling remaining={int(remaining.total_seconds())}s"
            # Cooldown over — clear and re-evaluate
            db = self.db_session_factory()
            try:
                from app.db.models import User as _User
                u = db.query(_User).filter(_User.id == user.id).first()
                if u is not None:
                    u.circuit_breaker_tripped_at = None
                    db.commit()
                    user.circuit_breaker_tripped_at = None
            finally:
                db.close()

        # Evaluate the last N closed trades for this user across this bot.
        db = self.db_session_factory()
        try:
            from app.db.models import Execution
            recent = (
                db.query(TradeOutcome)
                .join(Execution, Execution.signal_id == TradeOutcome.signal_id)
                .filter(
                    TradeOutcome.bot_id == self.bot_id,
                    Execution.user_id == user.id,
                )
                .order_by(TradeOutcome.id.desc())
                .limit(n_losses)
                .all()
            )
            if len(recent) < n_losses:
                return True, None  # not enough history yet
            if all(t.pnl_usd is not None and t.pnl_usd < 0 for t in recent):
                # Trip it
                from app.db.models import User as _User
                u = db.query(_User).filter(_User.id == user.id).first()
                if u is not None:
                    u.circuit_breaker_tripped_at = datetime.utcnow()
                    db.commit()
                    user.circuit_breaker_tripped_at = u.circuit_breaker_tripped_at
                return False, f"circuit_breaker_tripped {n_losses} consecutive losses"
            return True, None
        finally:
            db.close()

    def _check_live_guardrails(
        self, user: User, account_state: dict | None
    ) -> tuple[bool, str | None]:
        """Pre-flight risk check for live accounts. Returns (allowed, reason).

        Triggers a halt when:
          - daily_kill_switch_pct is set AND today's net (equity − balance)
            has exceeded that loss threshold
          - account_state is missing required fields (broker likely down)

        Demo accounts and users without overrides pass through with allowed=True.
        """
        if user.tradelocker_env != "live":
            return True, None
        kill_pct = float(user.daily_kill_switch_pct or 0.0)
        if kill_pct <= 0:
            return True, None
        # Fail-soft on broker hiccups. The lot cap + position cap + exhaustion
        # filter still bound risk per trade, so a transient null balance
        # shouldn't shut the bot off.
        if not account_state:
            return True, None
        # TradeLockerClient.get_account_state returns camelCase keys directly
        # from STATE_COL. The HTTP-API layer (/api/tradelocker/account) maps
        # these to snake_case, but the runner calls the client directly so we
        # MUST use the original keys. Bug 2026-05-10: previously checked
        # account_state.get("today_net") which is always None, silently
        # disabling the kill switch.
        balance_raw = account_state.get("balance")
        today_raw = account_state.get("todayNet")
        if balance_raw is None or today_raw is None:
            return True, None
        balance = float(balance_raw)
        today_net = float(today_raw)
        if balance <= 0:
            return True, None
        loss_pct = (-today_net / balance) * 100.0 if today_net < 0 else 0.0
        if loss_pct >= kill_pct:
            return (
                False,
                f"daily_kill_switch_hit loss={loss_pct:.2f}% >= cap {kill_pct:.2f}%",
            )
        return True, None

    def _count_user_open_cohorts(self, tm_db: Session, user_id: int) -> int:
        """Total open cohorts for this user across all symbols on this bot.

        DEPRECATED for cap-enforcement (see _count_broker_positions). Still
        used by some legacy paths; kept for backwards-compat.
        """
        from app.db.models import Cohort, CohortStatus

        q = tm_db.query(Cohort).filter(
            Cohort.user_id == user_id,
            Cohort.bot_id == self.bot_id,
            Cohort.status != CohortStatus.closed,
        )
        return q.count()

    async def _count_broker_positions(
        self,
        client: TradeLockerClient,
        account_id: str,
        token: str,
        acc_num: str,
    ) -> tuple[int, dict[int, list[dict]]]:
        """Live broker position count + per-instrument grouping.

        SOURCE OF TRUTH for the position cap. Counts what's actually
        open at the broker, not what our DB thinks. Eliminates races
        between 1m/5m runners and catches manual broker positions.

        Returns:
          (total_count, positions_by_tradable_id)
        """
        try:
            positions = await client.get_positions(account_id, token, acc_num)
        except Exception as exc:
            logger.warning("broker position count failed: %s — assuming 0", exc)
            return 0, {}
        by_id: dict[int, list[dict]] = {}
        for p in positions:
            tid = int(p.get("tradableInstrumentId") or 0)
            by_id.setdefault(tid, []).append(p)
        return len(positions), by_id

    async def _tick(
        self,
        strategy: LatPFNQuantStrategy,
        bar_fetcher: BarFetcher,
        client: TradeLockerClient,
    ) -> None:
        threshold = self._read_threshold(default=0.5)
        for symbol in self.symbols:
            try:
                bars = await bar_fetcher.fetch_bars(symbol, self.timeframe, count=240)
            except Exception as exc:
                logger.warning("bars fetch failed %s: %s", symbol, exc)
                self._log_tick(symbol, "error", reason=f"bars fetch failed: {exc}", threshold=threshold)
                continue

            # CRITICAL: never trade on synthetic fallback data. The data_feed
            # marks synthetic frames with df.attrs['synthetic'] = True. If the
            # broker is unreachable, we'd rather skip the tick than fire on
            # fabricated prices (we lost real money to phantom trades — see
            # the $118 BTC trade in trade_log on 2026-05-10).
            synthetic_now = bool(getattr(bars, "attrs", {}).get("synthetic"))
            if synthetic_now:
                self._log_tick(
                    symbol,
                    "skip_synthetic_data",
                    threshold=threshold,
                    reason="bars are synthetic (broker unreachable) — refusing to trade",
                )
                # Fire health alert if synthetic streak exceeds threshold
                try:
                    from app.monitoring.alerts import maybe_alert
                    asyncio.ensure_future(maybe_alert(f"synthetic_{symbol}", failure=True))
                except Exception as exc:
                    logger.debug("alert publish failed: %s", exc)
                continue
            else:
                # Healthy bar — reset the streak if it had been alerted
                try:
                    from app.monitoring.alerts import maybe_alert
                    asyncio.ensure_future(maybe_alert(f"synthetic_{symbol}", failure=False))
                except Exception:
                    pass

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
                            # Map command kind → tick decision for Discord/WS fanout.
                            # modify_sl (routine trailing) is suppressed to keep noise down;
                            # everything else is high-signal and gets posted.
                            decision_map = {
                                "scale_in": "scale_in",
                                "partial_close": "partial_close",
                                "exit_all": "hard_exit",
                            }
                            decision = decision_map.get(cmd.kind)
                            if decision:
                                self._log_tick(
                                    symbol,
                                    decision,
                                    current_price=current_price,
                                    forecast_view=forecast_view,
                                    threshold=threshold,
                                    reason=cmd.reason or cmd.kind,
                                    user_id=user.id,
                                    extra={
                                        "qty": cmd.qty,
                                        "sl": cmd.new_stop,
                                        "tp": cohort.initial_take_profit,
                                        "cohort_id": cohort.id,
                                        "side": cohort.side,
                                    },
                                )
                        except Exception as exc:
                            tm_db.rollback()
                            logger.warning("cmd %s on cohort %s failed: %s", cmd.kind, cohort.id, exc)
                    # 2) Look for new entries (broker-truth gating).
                    # We always fetch broker positions before entry so cap +
                    # exposure decisions are based on the actual broker state,
                    # not just our DB. This eliminates 1m/5m races and catches
                    # manual positions opened outside the bot.
                    if not open_cohorts:
                        sig = await strategy.on_bar(symbol, bars)
                        if sig is not None and sig.extra.get("kind") == "entry":
                            # ---- Step 1: Global panic switch ----
                            if getattr(user, "bot_paused", False):
                                self._log_tick(
                                    symbol, "skip_kill_switch",
                                    current_price=current_price,
                                    forecast_view=forecast_view,
                                    threshold=threshold,
                                    reason="user.bot_paused=True (global panic)",
                                    user_id=user.id,
                                )
                                continue

                            # ---- Step 2: Kill switch (daily DD) ----
                            account_state = None
                            try:
                                account_state = await client.get_account_state(
                                    user.tradelocker_account_id, token, user.tradelocker_acc_num or "1"
                                )
                            except Exception as exc:
                                logger.debug("account state fetch failed: %s", exc)

                            allowed, ks_reason = self._check_live_guardrails(user, account_state)
                            if not allowed:
                                self._log_tick(
                                    symbol, "skip_kill_switch",
                                    current_price=current_price,
                                    forecast_view=forecast_view,
                                    threshold=threshold,
                                    reason=ks_reason or "kill switch",
                                    user_id=user.id,
                                )
                                continue

                            # ---- Step 2b: Circuit breaker (N consecutive losses) ----
                            cb_allowed, cb_reason = self._check_circuit_breaker(user)
                            if not cb_allowed:
                                self._log_tick(
                                    symbol, "skip_kill_switch",
                                    current_price=current_price,
                                    forecast_view=forecast_view,
                                    threshold=threshold,
                                    reason=cb_reason or "circuit_breaker",
                                    user_id=user.id,
                                )
                                continue

                            # ---- Step 3: Broker truth ONE fetch for both
                            # position cap + exposure check ----
                            broker_total, by_tid = await self._count_broker_positions(
                                client,
                                user.tradelocker_account_id,
                                token,
                                user.tradelocker_acc_num or "1",
                            )

                            cap = int(user.max_concurrent_positions or 0)
                            if cap > 0 and broker_total >= cap:
                                self._log_tick(
                                    symbol, "skip_position_cap",
                                    current_price=current_price,
                                    forecast_view=forecast_view,
                                    threshold=threshold,
                                    reason=f"broker_positions={broker_total} >= cap {cap}",
                                    user_id=user.id,
                                )
                                continue

                            # ---- Step 4: Broker-level exposure on THIS symbol ----
                            try:
                                tradable_id, _ = await client.resolve_symbol(
                                    user.tradelocker_account_id,
                                    token,
                                    user.tradelocker_acc_num or "1",
                                    symbol,
                                )
                                if tradable_id in by_tid and by_tid[tradable_id]:
                                    self._log_tick(
                                        symbol, "skip_existing_position",
                                        current_price=current_price,
                                        forecast_view=forecast_view,
                                        threshold=threshold,
                                        reason=f"broker has {len(by_tid[tradable_id])} {symbol} position(s)",
                                        user_id=user.id,
                                    )
                                    continue
                            except Exception as exc:
                                logger.debug("symbol resolution failed for %s: %s", symbol, exc)

                            if user.exhaustion_filter_enabled:
                                # 'exhaustion_filter_enabled' is a legacy flag —
                                # now routed through the gentler no-chase filter
                                # which only blocks obvious top/bottom chasing
                                # rather than requiring a full reversal pattern.
                                ok, diag = passes_no_chase(bars, sig.side)
                                if not ok:
                                    self._log_tick(
                                        symbol, "skip_exhaustion_filter",
                                        current_price=current_price,
                                        forecast_view=forecast_view,
                                        threshold=threshold,
                                        reason=f"no_chase: {diag.get('reason')}",
                                        user_id=user.id,
                                        extra=diag,
                                    )
                                    continue

                            # Apply hard lot cap for tiny live accounts.
                            if user.max_lot_override is not None and user.max_lot_override > 0:
                                sig.qty = min(sig.qty, float(user.max_lot_override))

                            try:
                                await self._open_new_cohort(tm, sig, user, token, client)
                                tm_db.commit()
                                self._log_tick(
                                    symbol,
                                    f"entry_{sig.side}",
                                    current_price=current_price,
                                    forecast_view=forecast_view,
                                    threshold=threshold,
                                    reason=f"signal fired qty={sig.qty}",
                                    user_id=user.id,
                                    extra={"qty": sig.qty, "sl": sig.stop_loss, "tp": sig.take_profit},
                                )
                            except Exception as exc:
                                tm_db.rollback()
                                logger.warning("open cohort failed: %s", exc)
                                self._log_tick(
                                    symbol, "error",
                                    current_price=current_price,
                                    forecast_view=forecast_view,
                                    threshold=threshold,
                                    reason=f"open cohort failed: {exc}",
                                    user_id=user.id,
                                )
                        else:
                            # Forecast computed but didn't pass threshold — log it.
                            conf = (forecast_view or {}).get("confidence")
                            self._log_tick(
                                symbol, "skip_below_threshold",
                                current_price=current_price,
                                forecast_view=forecast_view,
                                threshold=threshold,
                                reason=(
                                    f"confidence {conf:.3f} < threshold {threshold:.2f}"
                                    if conf is not None
                                    else "no forecast available"
                                ),
                                user_id=user.id,
                            )
                    else:
                        # Existing cohort being managed — log a brief manage tick.
                        self._log_tick(
                            symbol, "manage",
                            current_price=current_price,
                            forecast_view=forecast_view,
                            threshold=threshold,
                            reason=f"managing {len(open_cohorts)} open cohort(s)",
                            user_id=user.id,
                        )
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
        account_id = user.tradelocker_account_id
        if not account_id:
            raise ValueError(f"user {user.id} has no tradelocker_account_id")
        atr = float(sig.extra.get("atr", 0.0))
        # Idempotency key: deterministic across retries within the same minute
        # but unique per (user, symbol, side, minute). If the runner retries
        # because of a transient broker failure, the same key prevents a
        # second position from opening. Bucket size 60s aligns with the 1m
        # bar cadence — within a single bar we cannot reasonably fire twice.
        import time as _time
        _bucket = int(_time.time() // 60)
        client_order_id = f"tc-entry-{self.bot_id}-{user.id}-{sig.symbol}-{sig.side}-{_bucket}"
        logger.info(
            "place_order request: %s %s qty=%.4f entry=%.2f SL=%.2f TP=%.2f coid=%s",
            sig.side, sig.symbol, sig.qty, sig.entry_price, sig.stop_loss or 0.0, sig.take_profit or 0.0, client_order_id,
        )
        order = await client.place_order(
            account_id=account_id,
            token=token,
            acc_num=user.tradelocker_acc_num or "1",
            symbol=sig.symbol,
            side=sig.side,
            qty=sig.qty,
            sl=sig.stop_loss,
            client_order_id=client_order_id,
            tp=sig.take_profit,
        )
        order_id = str(order.get("order_id") or "")
        logger.info("place_order response: %s", order)

        # Find the position id. Robust matching priority:
        #   1. orderId match (most precise — links position back to order we sent)
        #   2. strategyId match (some TL deployments echo this)
        #   3. Most recent position on this symbol+side with no prior cohort
        #      tracking it (newest first via openDate).
        # Bug 2026-05-10: previous code took "last position with matching
        # side", which on a hedging account with multiple same-direction
        # positions would link to a pre-existing trade.
        positions = await client.get_positions(
            account_id, token, user.tradelocker_acc_num or "1"
        )

        # Resolve target tradable_id once
        target_tid = None
        try:
            target_tid, _ = await client.resolve_symbol(
                account_id, token, user.tradelocker_acc_num or "1", sig.symbol,
            )
        except Exception:
            pass

        pos_id = None
        pos_obj: dict | None = None
        # Priority 1+2: match by orderId or strategyId
        for p in positions:
            p_order = str(p.get("orderId") or p.get("order_id") or "")
            p_strat = str(p.get("strategyId") or p.get("strategy_id") or "")
            if order_id and (p_order == order_id or p_strat == order_id):
                pos_id = str(p.get("id"))
                pos_obj = p
                break
        # Priority 3: newest matching symbol/side, sorted by openDate desc
        if pos_id is None:
            same_symbol_side = [
                p for p in positions
                if str(p.get("side", "")).lower() == sig.side.lower()
                and (target_tid is None or int(p.get("tradableInstrumentId") or 0) == target_tid)
            ]
            # Sort by openDate desc if available; else assume list is chronological
            try:
                same_symbol_side.sort(key=lambda p: p.get("openDate") or 0, reverse=True)
            except Exception:
                pass
            if same_symbol_side:
                pos_obj = same_symbol_side[0]
                pos_id = str(pos_obj.get("id"))

        # Verify SL/TP made it onto the broker. If not, repair via modify_position.
        # Some TL deployments silently drop sl/tp on the order create — we send
        # a follow-up modify so every entry has a stop. Without this we get
        # uncovered positions like the 2026-05-10 hedged ETH mess.
        if pos_id and pos_obj:
            broker_sl = pos_obj.get("stopLoss") or pos_obj.get("stop_loss")
            broker_tp = pos_obj.get("takeProfit") or pos_obj.get("take_profit")
            needs_sl_repair = (
                sig.stop_loss is not None
                and (broker_sl is None or float(broker_sl) == 0.0)
            )
            needs_tp_repair = (
                sig.take_profit is not None
                and (broker_tp is None or float(broker_tp) == 0.0)
            )
            if needs_sl_repair or needs_tp_repair:
                logger.warning(
                    "broker dropped sl=%s tp=%s on order — repairing pos=%s",
                    broker_sl, broker_tp, pos_id,
                )
                try:
                    await client.modify_position(
                        pos_id,
                        token=token,
                        acc_num=user.tradelocker_acc_num or "1",
                        stop_loss=sig.stop_loss if needs_sl_repair else None,
                        take_profit=sig.take_profit if needs_tp_repair else None,
                    )
                except Exception as exc:
                    logger.error("SL/TP repair failed for pos %s: %s — closing immediately", pos_id, exc)
                    try:
                        await client.close_position(
                            pos_id,
                            token=token,
                            acc_num=user.tradelocker_acc_num or "1",
                        )
                    except Exception as exc2:
                        logger.exception("emergency close failed pos=%s: %s", pos_id, exc2)
                    raise RuntimeError(f"position {pos_id} could not have SL set; closed defensively")
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
        cohort: Any,
        cmd: CohortCommand,
        user: User,
        token: str,
        client: TradeLockerClient,
        current_price: float,
    ) -> None:
        from app.db.models import Cohort  # avoid circular at import time

        assert isinstance(cohort, Cohort)
        account_id = user.tradelocker_account_id
        if not account_id:
            raise ValueError(f"user {user.id} has no tradelocker_account_id")
        if cmd.kind == "scale_in":
            if cmd.new_stop is None:
                raise ValueError("scale_in command missing new_stop")
            # Idempotency key for scale-in legs: bucketed by minute, includes
            # leg count so a 2nd scale-in within the same minute gets its own
            # key (rare in practice, but the math should be sound).
            import time as _time
            _scale_bucket = int(_time.time() // 60)
            _leg_count = len([l for l in cohort.legs if l.is_open])
            scale_coid = f"tc-scalein-{cohort.id}-{_leg_count}-{_scale_bucket}"
            order = await client.place_order(
                account_id=account_id,
                token=token,
                acc_num=user.tradelocker_acc_num or "1",
                client_order_id=scale_coid,
                symbol=cohort.instrument,
                side=cohort.side,
                qty=cmd.qty,
                sl=cmd.new_stop,
                tp=cohort.initial_take_profit,
            )
            order_id = str(order.get("order_id") or "")
            positions = await client.get_positions(
                account_id, token, user.tradelocker_acc_num or "1"
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
            # Pick the first OPEN leg with a valid broker position ID.
            # legs[0] is the original entry but may have been already closed
            # or never linked to a broker position (e.g. order placed but
            # lookup race). Falling back to any open leg with a position ID.
            close_leg = next(
                (
                    l
                    for l in cohort.legs
                    if l.is_open
                    and l.role in ("entry", "scale_in")
                    and l.tradelocker_position_id
                ),
                None,
            )
            if close_leg is None:
                logger.warning(
                    "partial_close skipped for cohort %s: no open leg with broker position id",
                    cohort.id,
                )
                return
            position_id = close_leg.tradelocker_position_id
            if not position_id:
                logger.warning(
                    "partial_close skipped for cohort %s: leg has no broker position id",
                    cohort.id,
                )
                return
            close_resp = await client.partial_close(
                account_id=account_id,
                token=token,
                acc_num=user.tradelocker_acc_num or "1",
                position_id=position_id,
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

    def _read_threshold(self, default: float = 0.5) -> float:
        """Read confidence_threshold from StrategyState; never raise."""
        try:
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
                if state and state.confidence_threshold:
                    return float(state.confidence_threshold)
            finally:
                db.close()
        except Exception as exc:
            logger.warning("read threshold failed (using default %s): %s", default, exc)
        return default

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

    def _safe_state_update(self, mutator: Callable[[Any], None], label: str) -> None:
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
                        "user_id": u.id,
                        "account_id": u.tradelocker_account_id,
                        "token": token,
                        "acc_num": u.tradelocker_acc_num or "1",
                        "env": u.tradelocker_env or "demo",
                    }
            return None
        finally:
            db.close()
