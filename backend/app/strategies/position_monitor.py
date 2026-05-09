"""PositionMonitor — watches open positions and records TradeOutcomes on close.

For v1 we sample positions periodically; when an Execution we previously
fired no longer appears in the user's open positions list (qty=0 or absent),
we treat it as closed and record a TradeOutcome.

MAE/MFE are best-effort: we update high-water marks while the trade is open.
For an MVP this is sufficient — the runner samples once per bar.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any, Optional

from sqlalchemy.orm import Session

from app.core.crypto import decrypt
from app.core.tradelocker_client import TradeLockerClient, TradeLockerError
from app.db.models import Execution, ExecutionStatus, Signal, TradeOutcome, User

logger = logging.getLogger(__name__)


class PositionMonitor:
    """Tracks (execution_id → high-water marks) until the position closes."""

    def __init__(self, db_session_factory, client: TradeLockerClient, bot_id: int, timeframe: str) -> None:
        self.db_session_factory = db_session_factory
        self.client = client
        self.bot_id = bot_id
        self.timeframe = timeframe
        # execution_id → {"mae": float, "mfe": float, "entry": float, "side": str, "opened_at": datetime}
        self._tracking: dict[int, dict[str, Any]] = {}

    def track(
        self,
        execution: Execution,
        entry_price: float,
        side: str,
        opened_at: Optional[datetime] = None,
    ) -> None:
        self._tracking[execution.id] = {
            "mae": 0.0,
            "mfe": 0.0,
            "entry": float(entry_price),
            "side": side,
            "opened_at": opened_at or datetime.utcnow(),
        }

    async def poll_and_close(self) -> list[TradeOutcome]:
        """Sample all tracked users' positions; close the ones that disappeared."""
        if not self._tracking:
            return []

        outcomes: list[TradeOutcome] = []
        db = self.db_session_factory()
        try:
            # Group tracked executions by user_id for batched API calls
            tracked_ids = list(self._tracking.keys())
            execs = (
                db.query(Execution)
                .filter(Execution.id.in_(tracked_ids))
                .all()
            )
            by_user: dict[int, list[Execution]] = {}
            for ex in execs:
                by_user.setdefault(ex.user_id, []).append(ex)

            for user_id, user_execs in by_user.items():
                user: User | None = db.get(User, user_id)
                if user is None:
                    continue
                token = decrypt(user.tradelocker_token) if user.tradelocker_token else None
                if not token or not user.tradelocker_account_id:
                    continue
                try:
                    positions = await self.client.get_positions(
                        user.tradelocker_account_id, token, user.tradelocker_acc_num or "1"
                    )
                except TradeLockerError as exc:
                    logger.warning("position poll failed for user=%s: %s", user_id, exc)
                    continue

                # Build a quick lookup: order_id → position
                open_order_ids = {
                    str(p.get("id")) for p in positions if float(p.get("qty", 0)) != 0
                }

                # Update MAE/MFE for still-open trades
                for ex in user_execs:
                    track = self._tracking.get(ex.id)
                    if track is None:
                        continue
                    pos = next(
                        (
                            p
                            for p in positions
                            if str(p.get("id")) == str(ex.tradelocker_order_id)
                        ),
                        None,
                    )
                    if pos is not None:
                        # Update high-water marks using avgPrice as a proxy for current
                        try:
                            cur = float(pos.get("avgPrice"))
                        except (TypeError, ValueError):
                            cur = track["entry"]
                        delta = cur - track["entry"]
                        if track["side"] == "sell":
                            delta = -delta
                        track["mfe"] = max(track["mfe"], delta)
                        track["mae"] = min(track["mae"], delta)
                        continue

                    # Position closed → build TradeOutcome
                    if ex.tradelocker_order_id and str(ex.tradelocker_order_id) in open_order_ids:
                        continue

                    outcome = await self._close_outcome(db, ex, track)
                    if outcome is not None:
                        outcomes.append(outcome)
                    self._tracking.pop(ex.id, None)
        finally:
            db.close()

        return outcomes

    async def _close_outcome(
        self,
        db: Session,
        ex: Execution,
        track: dict[str, Any],
    ) -> Optional[TradeOutcome]:
        signal: Signal | None = db.get(Signal, ex.signal_id)
        if signal is None:
            return None

        # We don't have a true exit fill price from the broker without trade
        # history — for v1 use SL or TP estimate based on which side won.
        # If the trade closed in profit, assume TP; otherwise assume SL.
        # MFE/MAE are tracked above and used to disambiguate.
        entry = track["entry"]
        side = track["side"]
        if track["mfe"] >= abs(track["mae"]):
            exit_price = float(signal.take_profit) if signal.take_profit else entry
        else:
            exit_price = float(signal.stop_loss) if signal.stop_loss else entry

        qty = float(ex.executed_lot_size or 0.0)
        pnl_per_unit = exit_price - entry if side == "buy" else entry - exit_price
        pnl_usd = pnl_per_unit * qty * 100_000.0  # rough FX standard-lot scaler; instrument-specific later

        # R-multiple = pnl_per_unit / risked_per_unit
        risked = abs(entry - float(signal.stop_loss)) if signal.stop_loss else 0.0
        r_multiple = pnl_per_unit / risked if risked > 1e-9 else 0.0

        opened_at = track["opened_at"]
        closed_at = datetime.utcnow()
        hold_seconds = max(0, int((closed_at - opened_at).total_seconds()))

        outcome = TradeOutcome(
            bot_id=self.bot_id,
            signal_id=signal.id,
            instrument=signal.instrument,
            side=side,
            timeframe=self.timeframe,
            entry_price=entry,
            exit_price=float(exit_price),
            qty=qty,
            pnl_usd=float(pnl_usd),
            r_multiple=float(r_multiple),
            forecast_drift=_extract_forecast(signal, "forecast_drift"),
            forecast_confidence=_extract_forecast(signal, "forecast_confidence"),
            threshold_at_entry=_extract_forecast(signal, "threshold"),
            opened_at=opened_at,
            closed_at=closed_at,
            hold_seconds=hold_seconds,
            mae=float(track["mae"]),
            mfe=float(track["mfe"]),
        )
        db.add(outcome)
        db.commit()
        return outcome


def _extract_forecast(signal: Signal, key: str) -> Optional[float]:
    if not signal.raw_payload:
        return None
    try:
        data = json.loads(signal.raw_payload)
    except (ValueError, TypeError):
        return None
    val = data.get(key)
    return float(val) if val is not None else None
