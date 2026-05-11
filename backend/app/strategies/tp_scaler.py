"""Confidence-σ → TP-% scaler for LaT-PFN strategies.

The legacy quant + momentum strategies clipped TP to a band of
±[0.5 × ATR, 3 × ATR] around entry. That treats all signals equally
regardless of how strong the forecast actually was — a 0.5σ "barely
above the threshold" signal got the same upside cap as a 2.5σ "high
conviction" signal.

This module replaces that with a confidence-driven curve. Stronger
forecasts get larger % TP targets; the SL is derived from the user's
target R:R so the math stays internally consistent.

The curve has stepped tiers (for readability) interpolated linearly
between anchor points:

    confidence (σ)    target TP %    notes
    -----------------------------------------
    < 0.3             — (no entry)   below floor, see threshold
    0.3 – 1.0         1.5%           weak edge, scalp it
    1.0 – 1.5         3.0%           moderate edge
    1.5 – 2.5         6.0%           strong edge, let it work
    ≥ 2.5             10.0%          high conviction, full target
                                     (capped at +12% to avoid silly TPs
                                      on low-volatility instruments)

The risk_appetite preset (from tiny_account_advisor.PRESETS) scales
both the entry threshold AND the target R:R, which in turn scales the
SL distance.
"""
from __future__ import annotations

from dataclasses import dataclass


# Anchor points: (confidence_σ, target_tp_percent). The scaler linearly
# interpolates between adjacent anchors and clamps at the bounds.
_TP_CURVE: list[tuple[float, float]] = [
    (0.3, 1.5),
    (1.0, 3.0),
    (1.5, 6.0),
    (2.5, 10.0),
    (4.0, 12.0),  # ceiling — anything more aggressive feels like a glitch
]


def confidence_to_tp_pct(confidence: float) -> float:
    """Map confidence-σ to a target TP percentage move from entry.

    Pure function — no env, no state. Easy to test and to reason about.
    """
    if confidence <= _TP_CURVE[0][0]:
        return _TP_CURVE[0][1]
    if confidence >= _TP_CURVE[-1][0]:
        return _TP_CURVE[-1][1]
    for (lo_c, lo_tp), (hi_c, hi_tp) in zip(_TP_CURVE, _TP_CURVE[1:]):
        if lo_c <= confidence <= hi_c:
            # Linear interpolation between the two anchors.
            t = (confidence - lo_c) / (hi_c - lo_c) if hi_c > lo_c else 0.0
            return lo_tp + t * (hi_tp - lo_tp)
    return _TP_CURVE[-1][1]


@dataclass
class TPLevels:
    """Computed entry/SL/TP triangle for a single signal."""
    entry: float
    stop_loss: float
    take_profit: float
    tp_pct: float            # target % move from entry (informational)
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
      atr: current ATR — used as a noise floor for SL (we don't want
        the SL inside typical bar-to-bar noise; the trade would die to
        a tick instead of an actual reversal)
      confidence: drift-over-σ from the forecaster
      target_rr: desired reward-to-risk from the user's risk_appetite

    Returns: TPLevels with entry/SL/TP computed for `side`.
    """
    side_l = side.lower()
    is_buy = side_l == "buy"

    tp_pct = confidence_to_tp_pct(confidence)
    tp_dist = current_price * (tp_pct / 100.0)
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
        tp_pct=float(tp_pct),
        sl_atr_floor=float(min_sl_dist) if floor_engaged else 0.0,
        rr=float(rr),
    )
