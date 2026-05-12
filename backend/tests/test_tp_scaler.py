"""Tests for the confidence-σ → ATR-multiple scaler.

Curve was migrated from %-of-price to ATR-multiples on 2026-05-11 after
a backtest showed the % curve produced 0% win rate on 1m/5m bars.
Anchor values are now ATR multipliers (0.5×ATR at the floor, 3×ATR at
the ceiling).
"""
from __future__ import annotations

import pytest

from app.strategies.tp_scaler import (
    compute_tp_sl,
    confidence_to_tp_atr_mult,
    confidence_to_tp_pct,
)


@pytest.mark.parametrize("confidence,expected_mult_ge,expected_mult_le", [
    (0.0, 0.5, 0.5),     # below floor → floor value
    (0.3, 0.5, 0.5),     # exactly at floor
    (1.0, 1.0, 1.0),     # at anchor
    (1.5, 1.5, 1.5),     # at anchor
    (2.5, 2.0, 2.0),     # at anchor
    (5.0, 3.0, 3.0),     # above ceiling → ceiling
    (0.65, 0.5, 1.0),    # interpolated between 0.3 and 1.0
    (2.0, 1.5, 2.0),     # interpolated between 1.5 and 2.5
])
def test_confidence_curve_anchors_and_interpolation(
    confidence, expected_mult_ge, expected_mult_le
):
    """Spot-check anchor points + interpolated midpoints (ATR multiples)."""
    m = confidence_to_tp_atr_mult(confidence)
    assert expected_mult_ge <= m <= expected_mult_le


def test_legacy_pct_alias_returns_same_value():
    """The deprecated `_pct` alias must return the same value as the new
    `_atr_mult` function so any external callers haven't silently broken."""
    for conf in [0.0, 0.5, 1.0, 1.5, 2.0, 3.0, 5.0]:
        assert confidence_to_tp_pct(conf) == confidence_to_tp_atr_mult(conf)


def test_buy_tp_above_entry_sl_below_entry():
    """A long trade: TP must be above entry, SL below. TP distance is in
    ATR multiples now — 2σ confidence → ~1.75×ATR target."""
    lv = compute_tp_sl(
        side="buy",
        current_price=100.0,
        atr=1.0,
        confidence=2.0,
        target_rr=1.5,
    )
    assert lv.entry == 100.0
    assert lv.take_profit > lv.entry
    assert lv.stop_loss < lv.entry
    # TP should be at roughly 1.5–2.0 × ATR for 2σ (between 1.5 and 2.5 anchors).
    tp_dist = lv.take_profit - lv.entry
    assert 1.5 <= tp_dist <= 2.0


def test_sell_tp_below_entry_sl_above_entry():
    """A short trade: TP must be below entry, SL above. R:R stays positive."""
    lv = compute_tp_sl(
        side="sell",
        current_price=100.0,
        atr=1.0,
        confidence=2.0,
        target_rr=1.5,
    )
    assert lv.take_profit < lv.entry
    assert lv.stop_loss > lv.entry
    assert lv.rr > 0


def test_rr_honored_when_atr_floor_does_not_kick_in():
    """At normal ATR + target_rr that doesn't compress SL below the floor,
    the actual R:R equals the requested target_rr.

    confidence=2.0 → TP = ~1.75×ATR. For target_rr=2.0, SL = ~0.875×ATR
    which is well above the 0.5×ATR floor → no clamp engages, R:R hits 2.0.
    """
    lv = compute_tp_sl(
        side="buy",
        current_price=100.0,
        atr=1.0,           # ATR=1 so we can read levels directly
        confidence=2.0,    # → 1.75×ATR TP target
        target_rr=2.0,
    )
    assert pytest.approx(lv.rr, rel=0.01) == 2.0


def test_atr_floor_engages_when_sl_would_be_below_floor():
    """When target_rr is high enough that desired SL goes below 0.5×ATR,
    the floor clamps SL up — R:R falls below target. Better to survive
    noise than to die on a tick.

    TP at 2σ = 1.75×ATR. With target_rr=5, desired SL = 0.35×ATR (below
    floor). Floor clamps SL to 0.5×ATR → actual R:R = 1.75/0.5 = 3.5.
    """
    lv = compute_tp_sl(
        side="buy",
        current_price=100.0,
        atr=1.0,
        confidence=2.0,
        target_rr=5.0,
        sl_floor_atr_mult=0.5,
    )
    assert lv.sl_atr_floor > 0  # floor engaged
    assert lv.rr < 5.0           # actual R:R smaller than target


def test_higher_confidence_yields_wider_tp():
    """Monotonicity: more confident → bigger % move."""
    low = compute_tp_sl("buy", 100.0, 1.0, confidence=0.5, target_rr=1.5)
    high = compute_tp_sl("buy", 100.0, 1.0, confidence=3.0, target_rr=1.5)
    assert (high.take_profit - high.entry) > (low.take_profit - low.entry)


def test_target_rr_changes_sl_distance_not_tp():
    """SL is derived from R:R; TP stays the same for the same confidence."""
    a = compute_tp_sl("buy", 100.0, 0.1, confidence=2.0, target_rr=1.0)
    b = compute_tp_sl("buy", 100.0, 0.1, confidence=2.0, target_rr=3.0)
    assert pytest.approx(a.take_profit) == b.take_profit
    # b's SL is closer to entry (smaller absolute SL distance because R:R is tighter)
    a_sl_dist = a.entry - a.stop_loss
    b_sl_dist = b.entry - b.stop_loss
    assert b_sl_dist < a_sl_dist


def test_returned_tp_pct_field_matches_curve():
    """The TPLevels.tp_pct field must equal the curve's value (now an
    ATR multiplier, despite the legacy field name)."""
    for conf in [0.2, 0.5, 1.0, 1.5, 2.0, 2.5, 3.5]:
        lv = compute_tp_sl("buy", 100.0, 1.0, confidence=conf, target_rr=1.5)
        assert lv.tp_pct == confidence_to_tp_pct(conf)
