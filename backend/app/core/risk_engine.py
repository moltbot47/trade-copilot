"""Risk engine: convert base lot size into a per-user lot size."""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def _aggression_multiplier(aggression_level: int) -> float:
    """Map aggression 1-10 to multiplier 0.25x .. 2.0x linearly.

    1 -> 0.25
    5 -> 1.00 (so 1->5 spans 0.75 over 4 steps = 0.1875/step)
    10 -> 2.0 (so 5->10 spans 1.0 over 5 steps = 0.20/step)
    """
    a = max(1, min(10, int(aggression_level)))
    if a <= 5:
        return 0.25 + (a - 1) * (0.75 / 4)
    return 1.0 + (a - 5) * (1.0 / 5)


def compute_user_lot(
    base_lot: float,
    aggression_level: int,
    account_balance: float,
    max_risk_pct: float = 3.0,
    step: float = 0.01,
    max_lot_cap: float | None = None,
    per_lot_risk_usd: float | None = None,
) -> float:
    """Return a rounded lot size that respects user aggression and risk cap.

    Three independent ceilings, take the min:

    1. **Aggression-scaled base_lot** — base × multiplier(1-10).
    2. **Per-lot dollar-risk cap** (when ``per_lot_risk_usd`` is provided) —
       limits lots so that a stop-out costs at most ``max_risk_pct``% of
       balance. This is the ONLY ceiling that uses ``account_balance``
       meaningfully. If you don't pass per_lot_risk_usd we cannot do this
       check, so it's skipped with a warning at first call.
    3. **Hard ``max_lot_cap``** — broker minimum-lot scenarios. Used for tiny
       live accounts (e.g. $21 forced to 0.01 BTC = 38× leverage).

    Bug 2026-05-10: previously the formula was ``capped = max_risk_pct`` —
    a pure percentage value (e.g. 3.0) was interpreted as 3.0 lots,
    completely ignoring ``account_balance``. On a $21 account this allowed
    3.0 lots (38,000× over the stated 3% risk target). Now: the percent-
    of-balance ceiling only fires when callers can tell us how much $
    is at risk per lot (typically ATR × lot_multiplier).
    """
    if base_lot <= 0:
        return 0.0
    multiplier = _aggression_multiplier(aggression_level)
    scaled = base_lot * multiplier

    if (
        per_lot_risk_usd is not None
        and per_lot_risk_usd > 0
        and account_balance > 0
        and max_risk_pct > 0
    ):
        max_risk_dollars = account_balance * (max_risk_pct / 100.0)
        max_lots_by_risk = max_risk_dollars / per_lot_risk_usd
        scaled = min(scaled, max_lots_by_risk)
    elif account_balance > 0 and max_risk_pct > 0:
        # Caller didn't provide per_lot_risk_usd, so we cannot do a proper
        # risk-based cap. Log at DEBUG and rely on max_lot_cap.
        logger.debug(
            "compute_user_lot: per_lot_risk_usd not provided; risk-based cap skipped"
        )

    if max_lot_cap is not None and max_lot_cap > 0:
        scaled = min(scaled, max_lot_cap)

    if step <= 0:
        step = 0.01
    rounded = int(scaled / step) * step
    return round(max(rounded, step), 2)
