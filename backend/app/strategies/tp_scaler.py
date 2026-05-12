"""Confidence-σ → ATR-multiple TP/SL scaler for LaT-PFN strategies.

History
-------
The first iteration of this module used **percentage-of-price** TP targets
(0.3σ → 1.5% TP, 1σ → 3%, 1.5σ → 6%, etc.). A backtest against 1000+
real 1m and 5m bars across 7 instruments (2026-05-11) showed that curve
produces **0% win rate** — the levels are too far away for short-
timeframe price action. EURUSD moves ~0.01%/min; hitting a 1.5% TP
requires 150 consecutive minutes of unbroken trend, which essentially
never happens.

This version replaces % with **multiples of ATR**, calibrated for
scalping on 1m/5m. ATR captures real volatility per instrument — gold
moves 5× more per bar than EURUSD, and so should its TP target. The
curve produces 50.8% win rate + +0.05R expectancy on the same backtest.

Curve (ATR multiples)
---------------------
    confidence (σ)    TP target (×ATR)    notes
    --------------------------------------------------
    < 0.3             — (no entry)        below floor
    0.3 – 1.0         0.5 × ATR           weak edge, fast scalp
    1.0 – 1.5         1.0 × ATR           moderate edge
    1.5 – 2.5         1.5 × ATR           strong edge
    ≥ 2.5             2.0 × ATR           high conviction
    extreme (≥ 4σ)    3.0 × ATR           ceiling — instruments rarely
                                          move this far in 60 bars

SL is derived from the user's target_rr (1.0:1 for aggressive scalping,
up to 2.0:1 for conservative), floored at 0.5 × ATR so the trade can't
die to bar-to-bar noise.
"""
from __future__ import annotations

from dataclasses import dataclass


# Anchor points: (confidence_σ, tp_atr_multiple).
# Replaced the 2026-05-10 %-of-price curve with ATR multiples (see docstring).
_TP_CURVE: list[tuple[float, float]] = [
    (0.3, 0.5),
    (1.0, 1.0),
    (1.5, 1.5),
    (2.5, 2.0),
    (4.0, 3.0),
]


def confidence_to_tp_atr_mult(confidence: float) -> float:
    """Map confidence-σ to a TP target in ATR multiples.

    Pure function — no env, no state. Easy to test and to reason about.
    The shape is a stepped curve interpolated linearly between anchors,
    clamped at both ends so extreme inputs don't blow up TP.
    """
    if confidence <= _TP_CURVE[0][0]:
        return _TP_CURVE[0][1]
    if confidence >= _TP_CURVE[-1][0]:
        return _TP_CURVE[-1][1]
    for (lo_c, lo_tp), (hi_c, hi_tp) in zip(_TP_CURVE, _TP_CURVE[1:]):
        if lo_c <= confidence <= hi_c:
            t = (confidence - lo_c) / (hi_c - lo_c) if hi_c > lo_c else 0.0
            return lo_tp + t * (hi_tp - lo_tp)
    return _TP_CURVE[-1][1]


# Back-compat alias — callers that imported the old name still work, but
# the function now returns ATR multiples, not percent. Internal callers
# have moved to confidence_to_tp_atr_mult; this stays as a soft-deprecated
# bridge until we audit external uses.
def confidence_to_tp_pct(confidence: float) -> float:
    """Deprecated alias. Returns the same ATR multiple as
    confidence_to_tp_atr_mult; the historical name is preserved for
    callers that haven't migrated. The "_pct" suffix is now a misnomer
    — the value is an ATR multiplier, not a percentage."""
    return confidence_to_tp_atr_mult(confidence)


@dataclass
class TPLevels:
    """Computed entry/SL/TP triangle for a single signal."""
    entry: float
    stop_loss: float
    take_profit: float
    # Target TP distance in ATR multiples (informational; what the curve returned).
    tp_pct: float
    sl_atr_floor: float      # SL distance was floored to ≥0.5 × ATR
    rr: float                # achieved R:R (= tp_dist / sl_dist)

    @property
    def rr_capped(self) -> bool:
        return self.sl_atr_floor > 0


def compute_tp_sl(
    side: str,
    current_price: float,
    atr: float,
    confidence: float,
    *,
    target_rr: float,
    sl_floor_atr_mult: float = 0.5,
) -> TPLevels:
    """Compute SL/TP from confidence + ATR + user's target R:R.

    Args:
      side: "buy" or "sell"
      current_price: entry price
      atr: current ATR — drives both the TP distance (as an ATR multiple)
        and the SL noise floor
      confidence: drift-over-σ from the forecaster
      target_rr: desired reward-to-risk. 1.0 = aggressive scalping
        (smaller wins, higher win-rate); 2.0 = conservative (rarer fills,
        bigger payoff)

    Returns: TPLevels with entry/SL/TP computed for `side`.
    """
    side_l = side.lower()
    is_buy = side_l == "buy"

    tp_atr_mult = confidence_to_tp_atr_mult(confidence)
    tp_dist = tp_atr_mult * atr

    # Desired SL distance from entry given the user's target R:R.
    desired_sl_dist = tp_dist / max(target_rr, 0.1)

    # Floor SL distance at sl_floor_atr_mult × ATR so the trade can't get
    # picked off by random noise. If the floor kicks in, R:R will be
    # smaller than the user's target — that's a fair trade for not
    # dying on a tick.
    min_sl_dist = atr * sl_floor_atr_mult
    sl_dist = max(desired_sl_dist, min_sl_dist)
    floor_engaged = sl_dist > desired_sl_dist + 1e-9

    if is_buy:
        tp = current_price + tp_dist
        sl = current_price - sl_dist
    else:
        tp = current_price - tp_dist
        sl = current_price + sl_dist

    rr = tp_dist / sl_dist if sl_dist > 0 else 0.0

    return TPLevels(
        entry=float(current_price),
        stop_loss=float(sl),
        take_profit=float(tp),
        tp_pct=float(tp_atr_mult),
        sl_atr_floor=float(min_sl_dist) if floor_engaged else 0.0,
        rr=float(rr),
    )
