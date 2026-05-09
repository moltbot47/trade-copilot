"""TradeManager — manages cohorts of pyramided positions.

A *cohort* is a group of related positions opened by the quant strategy that
work together: an initial entry leg plus zero or more scale-in legs, plus
zero or more "hedge_close" legs that emulate partial closes on hedging
accounts.

Responsibilities:
  - Compute volume-weighted average entry across legs.
  - Decide when to scale in (price moves +0.5R favorable + forecast still on).
  - Decide when to scale out (first leg hits +1R / TP1 → close 50%).
  - Decide when to trail the SL (after partial close, ratchet up by 1×ATR).
  - Decide when to fully exit (price hits trailing SL, TP_final, drawdown
    limit, or forecast reversal).

The class is broker-agnostic — it returns *commands* (dicts) that the
runner translates into TradeLocker API calls. This keeps testing easy
(no mocked HTTP needed for state machine tests).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from sqlalchemy.orm import Session

from app.db.models import Cohort, CohortLeg, CohortStatus

logger = logging.getLogger(__name__)


# Tuning knobs — exposed as class attrs so tests can override.
SCALE_IN_R_THRESHOLD = 0.5         # +0.5R favorable
SCALE_OUT_R_THRESHOLD = 1.0        # +1.0R hits TP1 → close 50%
TRAIL_ATR_MULTIPLE = 1.0           # trail SL by 1×ATR
TRAIL_LOCK_MIN_R = 0.5             # once trailed past entry, lock min +0.5R
DRAWDOWN_ATR_LIMIT = 2.0           # cohort avg drawdown > 2×ATR → exit
SCALE_IN_MAX_LEGS = 3              # entry + 2 scale-ins (hard cap)
FORECAST_REVERSE_THRESHOLD = 1.5   # opposite-direction forecast > 1.5σ → exit


@dataclass
class CohortCommand:
    """A management action the runner should execute."""

    kind: str  # "scale_in" | "partial_close" | "modify_sl" | "exit_all"
    cohort_id: int
    qty: float = 0.0
    new_stop: Optional[float] = None
    reason: str = ""


def _r_distance(cohort: Cohort) -> float:
    """The original 1R distance — entry → initial SL."""
    return abs(cohort.initial_entry_price - cohort.initial_stop_loss)


def _favorable_move(cohort: Cohort, price: float) -> float:
    """How far in our favor the cohort is (in price units, signed)."""
    if cohort.side == "buy":
        return price - cohort.weighted_avg_entry
    return cohort.weighted_avg_entry - price


def _favorable_r(cohort: Cohort, price: float) -> float:
    r = _r_distance(cohort)
    if r < 1e-9:
        return 0.0
    return _favorable_move(cohort, price) / r


def weighted_average_entry(legs: list[CohortLeg]) -> float:
    """Volume-weighted average entry across open legs (excludes hedge_close legs)."""
    open_legs = [l for l in legs if l.is_open and l.role in ("entry", "scale_in")]
    total_qty = sum(l.qty for l in open_legs)
    if total_qty <= 1e-12:
        return 0.0
    weighted = sum(l.entry_price * l.qty for l in open_legs)
    return weighted / total_qty


def open_qty(legs: list[CohortLeg]) -> float:
    """Total long/short qty across open entry+scale_in legs."""
    return sum(l.qty for l in legs if l.is_open and l.role in ("entry", "scale_in"))


class TradeManager:
    """Cohort lifecycle controller.

    Persists state via SQLAlchemy. Caller is responsible for committing
    the session after invoking methods that mutate Cohort/CohortLeg rows.
    """

    def __init__(self, db: Session, bot_id: int, user_id: int, timeframe: str) -> None:
        self.db = db
        self.bot_id = bot_id
        self.user_id = user_id
        self.timeframe = timeframe

    # ---------- creation & queries ----------

    def open_cohort(
        self,
        instrument: str,
        side: str,
        entry_price: float,
        atr: float,
        qty: float,
        stop_loss: float,
        take_profit: float,
        tl_position_id: Optional[str] = None,
        tl_order_id: Optional[str] = None,
        forecast_drift: Optional[float] = None,
        forecast_confidence: Optional[float] = None,
    ) -> Cohort:
        cohort = Cohort(
            bot_id=self.bot_id,
            user_id=self.user_id,
            instrument=instrument,
            side=side,
            timeframe=self.timeframe,
            status=CohortStatus.open,
            atr_at_entry=float(atr),
            initial_entry_price=float(entry_price),
            initial_stop_loss=float(stop_loss),
            initial_take_profit=float(take_profit),
            weighted_avg_entry=float(entry_price),
            total_qty=float(qty),
            closed_qty=0.0,
            current_stop=float(stop_loss),
            trail_high_water=float(entry_price),
            realized_pnl=0.0,
            opened_at=datetime.utcnow(),
            last_action="open",
            forecast_drift=forecast_drift,
            forecast_confidence=forecast_confidence,
        )
        self.db.add(cohort)
        self.db.flush()

        leg = CohortLeg(
            cohort_id=cohort.id,
            role="entry",
            side=side,
            entry_price=float(entry_price),
            qty=float(qty),
            stop_loss=float(stop_loss),
            take_profit=float(take_profit),
            tradelocker_position_id=tl_position_id,
            tradelocker_order_id=tl_order_id,
            is_open=True,
            opened_at=datetime.utcnow(),
        )
        self.db.add(leg)
        self.db.flush()
        return cohort

    def add_scale_in_leg(
        self,
        cohort: Cohort,
        entry_price: float,
        qty: float,
        stop_loss: float,
        tl_position_id: Optional[str] = None,
        tl_order_id: Optional[str] = None,
    ) -> CohortLeg:
        leg = CohortLeg(
            cohort_id=cohort.id,
            role="scale_in",
            side=cohort.side,
            entry_price=float(entry_price),
            qty=float(qty),
            stop_loss=float(stop_loss),
            take_profit=cohort.initial_take_profit,
            tradelocker_position_id=tl_position_id,
            tradelocker_order_id=tl_order_id,
            is_open=True,
            opened_at=datetime.utcnow(),
        )
        self.db.add(leg)
        # Recompute weighted avg
        legs = list(cohort.legs) + [leg]
        cohort.weighted_avg_entry = weighted_average_entry(legs)
        cohort.total_qty = open_qty(legs)
        cohort.last_action = "scale_in"
        self.db.flush()
        return leg

    def record_partial_close(
        self,
        cohort: Cohort,
        qty_closed: float,
        close_price: float,
        tl_position_id: Optional[str] = None,
        tl_order_id: Optional[str] = None,
    ) -> CohortLeg:
        """Add a hedge_close leg that nets out `qty_closed` from the cohort."""
        # P&L = (close - avg) * qty for buy; (avg - close) * qty for sell
        avg = cohort.weighted_avg_entry
        if cohort.side == "buy":
            pnl_per_unit = close_price - avg
        else:
            pnl_per_unit = avg - close_price
        # Realised pnl scaling — display only (cohort cosmetic).
        realized = pnl_per_unit * qty_closed
        leg = CohortLeg(
            cohort_id=cohort.id,
            role="hedge_close",
            side=("sell" if cohort.side == "buy" else "buy"),
            entry_price=float(close_price),
            qty=float(qty_closed),
            stop_loss=None,
            take_profit=None,
            tradelocker_position_id=tl_position_id,
            tradelocker_order_id=tl_order_id,
            is_open=True,
            pnl_usd=float(realized),
            opened_at=datetime.utcnow(),
        )
        self.db.add(leg)
        cohort.closed_qty = float(cohort.closed_qty) + float(qty_closed)
        cohort.realized_pnl = float(cohort.realized_pnl) + float(realized)
        cohort.status = CohortStatus.partial
        cohort.last_action = "partial_close"
        self.db.flush()
        return leg

    def update_stop(self, cohort: Cohort, new_stop: float) -> None:
        cohort.current_stop = float(new_stop)
        for leg in cohort.legs:
            if leg.is_open and leg.role in ("entry", "scale_in"):
                leg.stop_loss = float(new_stop)
        cohort.last_action = "trail_stop"
        self.db.flush()

    def close_cohort(
        self, cohort: Cohort, close_price: float, reason: str = "exit_all"
    ) -> None:
        avg = cohort.weighted_avg_entry
        remaining_qty = max(0.0, float(cohort.total_qty) - float(cohort.closed_qty))
        if cohort.side == "buy":
            pnl_per_unit = close_price - avg
        else:
            pnl_per_unit = avg - close_price
        cohort.realized_pnl = float(cohort.realized_pnl) + float(pnl_per_unit * remaining_qty)
        cohort.closed_qty = float(cohort.total_qty)
        cohort.status = CohortStatus.closed
        cohort.closed_at = datetime.utcnow()
        cohort.last_action = reason
        for leg in cohort.legs:
            if leg.is_open:
                leg.is_open = False
                leg.closed_price = float(close_price)
                leg.closed_at = datetime.utcnow()
        self.db.flush()

    def list_open_cohorts(self, instrument: Optional[str] = None) -> list[Cohort]:
        q = self.db.query(Cohort).filter(
            Cohort.bot_id == self.bot_id,
            Cohort.user_id == self.user_id,
            Cohort.status != CohortStatus.closed,
        )
        if instrument:
            q = q.filter(Cohort.instrument == instrument)
        return q.all()

    # ---------- decision logic ----------

    def evaluate(
        self,
        cohort: Cohort,
        current_price: float,
        forecast_drift: float = 0.0,
        forecast_confidence: float = 0.0,
    ) -> Optional[CohortCommand]:
        """Inspect cohort + current market and return the next action (if any).

        Order of checks:
          1. Hard exit: drawdown breach, opposite-direction forecast, TP_final hit, SL hit.
          2. Trailing stop update (only after partial-close).
          3. Scale-out trigger (first time we cross +1R).
          4. Scale-in trigger (favorable +0.5R move + forecast still confirms).
        """
        atr = float(cohort.atr_at_entry)
        if atr <= 0:
            return None
        r = _r_distance(cohort)
        if r <= 0:
            return None

        favorable = _favorable_move(cohort, current_price)
        favorable_r = favorable / r if r > 0 else 0.0

        # Update trailing high-water mark (price extreme in our favor)
        if cohort.side == "buy" and current_price > (cohort.trail_high_water or 0):
            cohort.trail_high_water = float(current_price)
        elif cohort.side == "sell" and (
            cohort.trail_high_water == 0 or current_price < cohort.trail_high_water
        ):
            cohort.trail_high_water = float(current_price)

        # 1a. SL hit
        if cohort.side == "buy" and current_price <= cohort.current_stop:
            return CohortCommand(kind="exit_all", cohort_id=cohort.id, reason="sl_hit")
        if cohort.side == "sell" and current_price >= cohort.current_stop:
            return CohortCommand(kind="exit_all", cohort_id=cohort.id, reason="sl_hit")

        # 1b. TP_final hit
        if cohort.side == "buy" and current_price >= cohort.initial_take_profit:
            return CohortCommand(kind="exit_all", cohort_id=cohort.id, reason="tp_final_hit")
        if cohort.side == "sell" and current_price <= cohort.initial_take_profit:
            return CohortCommand(kind="exit_all", cohort_id=cohort.id, reason="tp_final_hit")

        # 1c. Drawdown breach (cohort-wide unrealised against avg entry)
        adverse = -favorable
        if adverse > DRAWDOWN_ATR_LIMIT * atr:
            return CohortCommand(
                kind="exit_all", cohort_id=cohort.id, reason="drawdown_breach"
            )

        # 1d. Forecast reversal
        signed_drift = forecast_drift if cohort.side == "buy" else -forecast_drift
        if (
            forecast_confidence > FORECAST_REVERSE_THRESHOLD
            and signed_drift < 0
        ):
            return CohortCommand(
                kind="exit_all", cohort_id=cohort.id, reason="forecast_reversed"
            )

        # 2. Trailing stop update (after partial close — i.e. status=partial)
        if cohort.status == CohortStatus.partial:
            new_stop = self._compute_trail_stop(cohort, current_price)
            if new_stop is not None and self._is_better_stop(cohort, new_stop):
                return CohortCommand(
                    kind="modify_sl",
                    cohort_id=cohort.id,
                    new_stop=new_stop,
                    reason="trail_atr",
                )

        # 3. Scale-out (only if still status=open AND we crossed +1R)
        if (
            cohort.status == CohortStatus.open
            and favorable_r >= SCALE_OUT_R_THRESHOLD
        ):
            qty_to_close = round(float(cohort.total_qty) * 0.5, 4)
            # ensure min lot 0.01
            qty_to_close = max(qty_to_close, 0.01)
            qty_to_close = min(qty_to_close, float(cohort.total_qty))
            return CohortCommand(
                kind="partial_close",
                cohort_id=cohort.id,
                qty=qty_to_close,
                new_stop=cohort.weighted_avg_entry,  # move SL to break-even on remainder
                reason="tp1_scale_out_50pct",
            )

        # 4. Scale-in (favorable +0.5R, leg cap not reached, forecast still on)
        active_legs = [l for l in cohort.legs if l.is_open and l.role in ("entry", "scale_in")]
        if (
            cohort.status == CohortStatus.open
            and len(active_legs) < SCALE_IN_MAX_LEGS
            and favorable_r >= SCALE_IN_R_THRESHOLD
            and signed_drift > 0  # forecast still confirms
            # only fire scale-in once per active-leg threshold to avoid spamming
            and (cohort.last_action or "") not in (
                "scale_in",
                "scale_in_pending",
            )
        ):
            qty = float(cohort.legs[0].qty)  # match initial leg size
            new_leg_sl = (
                current_price - cohort.atr_at_entry
                if cohort.side == "buy"
                else current_price + cohort.atr_at_entry
            )
            return CohortCommand(
                kind="scale_in",
                cohort_id=cohort.id,
                qty=qty,
                new_stop=new_leg_sl,
                reason=f"favorable_{favorable_r:.2f}R",
            )

        return None

    # ---------- helpers ----------

    def _compute_trail_stop(self, cohort: Cohort, current_price: float) -> Optional[float]:
        atr = float(cohort.atr_at_entry)
        avg = float(cohort.weighted_avg_entry)
        if cohort.side == "buy":
            naive = current_price - atr * TRAIL_ATR_MULTIPLE
            # lock min +0.5R if we're past break-even
            if current_price >= avg:
                naive = max(naive, avg + TRAIL_LOCK_MIN_R * _r_distance(cohort))
            return naive
        else:
            naive = current_price + atr * TRAIL_ATR_MULTIPLE
            if current_price <= avg:
                naive = min(naive, avg - TRAIL_LOCK_MIN_R * _r_distance(cohort))
            return naive

    def _is_better_stop(self, cohort: Cohort, new_stop: float) -> bool:
        """A stop update is only valid if it tightens (reduces risk) for our direction."""
        cur = float(cohort.current_stop)
        if cohort.side == "buy":
            return new_stop > cur
        return new_stop < cur
