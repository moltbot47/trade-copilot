"""Risk engine: convert base lot size into a per-user lot size."""
from __future__ import annotations


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
) -> float:
    """Return a rounded lot size that respects user aggression and risk cap.

    Conservative model: assume a 1.0 lot trade hitting its stop = ~1% of balance lost.
    So max allowed lot = (max_risk_pct / 1.0). We then take min(scaled, capped).
    """
    if base_lot <= 0:
        return 0.0
    multiplier = _aggression_multiplier(aggression_level)
    scaled = base_lot * multiplier

    if account_balance > 0 and max_risk_pct > 0:
        # 1.0 lot ~= 1% account risk on a typical FX stop
        capped = max_risk_pct  # in lots, since 1 lot = 1%
        scaled = min(scaled, capped)

    # round down to nearest step (broker increment)
    if step <= 0:
        step = 0.01
    rounded = int(scaled / step) * step
    return round(max(rounded, step), 2)
