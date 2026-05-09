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

logger = logging.getLogger(__name__)


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

    def _record_error(self, msg: str) -> None:
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
                state.last_error = msg
                db.commit()
        finally:
            db.close()

    def _touch_tick(self, _outcome_count: int) -> None:
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
                state.last_tick_at = datetime.utcnow()
                db.commit()
        finally:
            db.close()

    def _load_users(self, db: Session) -> list[User]:
        if not self.user_emails:
            return []
        return db.query(User).filter(User.email.in_(self.user_emails), User.is_active.is_(True)).all()

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
