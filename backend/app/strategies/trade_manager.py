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
from typing import Any, Optional

from sqlalchemy.orm import Session

from app.db.models import Cohort, CohortLeg, CohortStatus

logger = logging.getLogger(__name__)


def _ws_publish_sl_moved(
    *,
    user_id: int,
    position_id: Optional[str],
    symbol: str,
    side: str,
    old_sl: Optional[float],
    new_sl: float,
    favorable_R: float,
    reason: str,
) -> None:
    """Best-effort publish of an SL move onto the `positions` WS channel.

    The frontend ActivityLog already handles `kind: "sl_moved"` events.
    Silently no-op if the event bus or asyncio is unavailable so the
    ratchet itself is never blocked by telemetry.
    """
    try:
        import asyncio
        from app.ws.event_bus import event_bus

        payload = {
            "kind": "sl_moved",
            "position_id": position_id,
            "symbol": symbol,
            "side": side,
            "old_sl": old_sl,
            "new_sl": new_sl,
            "favorable_R": favorable_R,
            "reason": reason,
        }
        result = event_bus.publish("positions", user_id, payload)
        if asyncio.iscoroutine(result):
            asyncio.ensure_future(result)
    except Exception as exc:  # pragma: no cover
        logger.debug("ws publish sl_moved failed user=%s: %s", user_id, exc)


# Tuning knobs — exposed as class attrs so tests can override.
SCALE_IN_R_THRESHOLD = 0.5         # +0.5R favorable
SCALE_OUT_R_THRESHOLD = 1.0        # +1.0R hits TP1 → close 50%
TRAIL_ATR_MULTIPLE = 1.0           # trail SL by 1×ATR
TRAIL_LOCK_MIN_R = 0.5             # once trailed past entry, lock min +0.5R
DRAWDOWN_ATR_LIMIT = 2.0           # cohort avg drawdown > 2×ATR → exit
SCALE_IN_MAX_LEGS = 3              # entry + 2 scale-ins (hard cap)
FORECAST_REVERSE_THRESHOLD = 1.5   # opposite-direction forecast > 1.5σ → exit

# Trailing-ratchet tuning (Wave 5B).
RATCHET_TRIGGER_R = 1.5            # ratchet activates only after price reaches >= 1.5R
RATCHET_FOLLOW_DISTANCE_R = 1.0    # SL trails 1R behind the high-water mark
RATCHET_MAX_R_CAP = 5.0            # hard cap on the ratcheted SL R-multiple


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

    def __init__(
        self,
        db: Session,
        bot_id: int,
        user_id: int,
        timeframe: str,
        client: Optional[Any] = None,
        token: Optional[str] = None,
        acc_num: Optional[str] = None,
    ) -> None:
        self.db = db
        self.bot_id = bot_id
        self.user_id = user_id
        self.timeframe = timeframe
        # Optional broker context — required only for live methods like
        # update_trailing_ratchet that call modify_position directly.
        self.client = client
        self.token = token
        self.acc_num = acc_num

    def set_broker_context(
        self, client: Any, token: str, acc_num: str
    ) -> None:
        """Attach a TradeLockerClient + auth so live methods can call the broker."""
        self.client = client
        self.token = token
        self.acc_num = acc_num

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

    def update_stop(self, cohort: Cohort, new_stop: float, reason: str = "trail_stop") -> None:
        prev_stop = cohort.current_stop
        cohort.current_stop = float(new_stop)
        moved_legs: list[tuple[Optional[str], Optional[float]]] = []
        for leg in cohort.legs:
            if leg.is_open and leg.role in ("entry", "scale_in"):
                old = float(leg.stop_loss) if leg.stop_loss is not None else None
                leg.stop_loss = float(new_stop)
                moved_legs.append((leg.tradelocker_position_id, old))
        cohort.last_action = reason
        self.db.flush()
        # Publish each moved leg as a separate WS event so the ActivityLog
        # renders an SL row per position.
        avg_entry = cohort.weighted_avg_entry
        favorable_r = (
            (float(new_stop) - avg_entry) / max(_r_distance(cohort), 1e-9)
            if cohort.side == "buy"
            else (avg_entry - float(new_stop)) / max(_r_distance(cohort), 1e-9)
        )
        for pos_id, old_sl in moved_legs:
            _ws_publish_sl_moved(
                user_id=cohort.user_id,
                position_id=pos_id,
                symbol=cohort.instrument,
                side=cohort.side,
                old_sl=old_sl,
                new_sl=float(new_stop),
                favorable_R=favorable_r,
                reason=reason,
            )

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
            # First update high-water mark for the ratchet
            prior_hw = float(cohort.max_favorable_r_seen or 0.0)
            if favorable_r > prior_hw:
                cohort.max_favorable_r_seen = float(favorable_r)
            hw_r = float(cohort.max_favorable_r_seen or 0.0)

            ratchet_stop = self._ratchet_target_sl(cohort, hw_r)
            atr_stop = self._compute_trail_stop(cohort, current_price)

            # Pick the tighter of the two trailing methods.
            candidates = [s for s in (ratchet_stop, atr_stop) if s is not None]
            if candidates:
                if cohort.side == "buy":
                    new_stop = max(candidates)
                else:
                    new_stop = min(candidates)
                if self._is_better_stop(cohort, new_stop):
                    reason = (
                        "trail_ratchet"
                        if ratchet_stop is not None and new_stop == ratchet_stop
                        else "trail_atr"
                    )
                    return CohortCommand(
                        kind="modify_sl",
                        cohort_id=cohort.id,
                        new_stop=new_stop,
                        reason=reason,
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

    # ---------- Wave 5B: trailing-take-profit ratchet ----------

    def _ratchet_target_sl(
        self, cohort: Cohort, favorable_r: float
    ) -> Optional[float]:
        """Compute the SL price the ratchet wants, given the favorable R-multiple.

        Spec: when max_favorable_R >= 1.5, target_sl_R = max_favorable_R - 1.0.
        At +1.5R → SL +0.5R, at +2R → SL +1R, at +3R → SL +2R, capped at +5R.
        Returns None if the trigger has not been crossed.
        """
        if favorable_r < RATCHET_TRIGGER_R:
            return None
        target_sl_r = favorable_r - RATCHET_FOLLOW_DISTANCE_R
        target_sl_r = min(target_sl_r, RATCHET_MAX_R_CAP)
        if target_sl_r <= 0:
            return None
        r = _r_distance(cohort)
        avg = float(cohort.weighted_avg_entry)
        if cohort.side == "buy":
            return avg + target_sl_r * r
        return avg - target_sl_r * r

    async def update_trailing_ratchet(
        self, symbol: str, current_price: float
    ) -> list[dict]:
        """Per-leg SL ratchet for cohorts in 'partial' (trailing) status.

        Inspects every open cohort in 'partial' state for the given symbol,
        updates `max_favorable_r_seen` if a new high-water mark is reached,
        computes a target SL trailing 1R behind the high-water R-multiple,
        and pushes that SL to the broker per leg if it tightens. Never
        loosens an existing SL.

        Returns: list of {"leg_id", "old_sl", "new_sl", "favorable_R"} for
        each leg whose SL was actually moved.
        """
        updates: list[dict] = []
        cohorts = [
            c
            for c in self.list_open_cohorts(symbol)
            if c.status == CohortStatus.partial
        ]
        for cohort in cohorts:
            r = _r_distance(cohort)
            if r <= 0:
                continue
            favorable_r = _favorable_r(cohort, current_price)
            prior_hw = float(cohort.max_favorable_r_seen or 0.0)
            if favorable_r > prior_hw:
                cohort.max_favorable_r_seen = float(favorable_r)
            hw_r = float(cohort.max_favorable_r_seen or 0.0)
            target_sl = self._ratchet_target_sl(cohort, hw_r)
            if target_sl is None:
                continue

            for leg in cohort.legs:
                if not leg.is_open or leg.role not in ("entry", "scale_in"):
                    continue
                old_sl = float(leg.stop_loss) if leg.stop_loss is not None else None
                # Skip if the proposed SL would NOT tighten — never lower SL on
                # a long, never raise SL on a short.
                if old_sl is not None:
                    if cohort.side == "buy" and target_sl <= old_sl:
                        continue
                    if cohort.side == "sell" and target_sl >= old_sl:
                        continue

                if (
                    self.client is not None
                    and leg.tradelocker_position_id
                    and self.token
                    and self.acc_num
                ):
                    try:
                        await self.client.modify_position(
                            leg.tradelocker_position_id,
                            token=self.token,
                            acc_num=self.acc_num,
                            stop_loss=float(target_sl),
                        )
                    except Exception as exc:
                        logger.warning(
                            "ratchet broker modify_position failed leg=%s: %s",
                            leg.id,
                            exc,
                        )
                        continue

                leg.stop_loss = float(target_sl)
                if cohort.side == "buy":
                    cohort.current_stop = max(
                        float(cohort.current_stop or 0.0), float(target_sl)
                    )
                else:
                    cur = float(cohort.current_stop or float("inf"))
                    cohort.current_stop = (
                        float(target_sl) if target_sl < cur else cur
                    )
                cohort.last_action = "trail_ratchet"
                # Publish to WS so /strategy ActivityLog renders an SL row.
                # Best-effort — never let a publish failure block the ratchet.
                _ws_publish_sl_moved(
                    user_id=cohort.user_id,
                    position_id=leg.tradelocker_position_id,
                    symbol=cohort.instrument,
                    side=cohort.side,
                    old_sl=old_sl,
                    new_sl=float(target_sl),
                    favorable_R=float(hw_r),
                    reason="trail_ratchet",
                )
                updates.append(
                    {
                        "leg_id": leg.id,
                        "old_sl": old_sl,
                        "new_sl": float(target_sl),
                        "favorable_R": float(hw_r),
                    }
                )
                logger.debug(
                    "ratchet leg=%s old_sl=%s new_sl=%.4f hw_R=%.3f",
                    leg.id,
                    old_sl,
                    target_sl,
                    hw_r,
                )

        if updates:
            self.db.flush()
        return updates
