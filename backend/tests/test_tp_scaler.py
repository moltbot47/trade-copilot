"""Tests for the confidence-σ → TP-% scaler."""
from __future__ import annotations

import pytest

from app.strategies.tp_scaler import compute_tp_sl, confidence_to_tp_pct


@pytest.mark.parametrize("confidence,expected_tp_pct_ge,expected_tp_pct_le", [
    (0.0, 1.5, 1.5),     # below floor → floor value
    (0.3, 1.5, 1.5),     # exactly at floor
    (1.0, 3.0, 3.0),     # at anchor
    (1.5, 6.0, 6.0),     # at anchor
    (2.5, 10.0, 10.0),   # at anchor
    (5.0, 12.0, 12.0),   # above ceiling → ceiling
    (0.65, 1.5, 3.0),    # interpolated between 0.3 and 1.0
    (2.0, 6.0, 10.0),    # interpolated between 1.5 and 2.5
])
def test_confidence_curve_anchors_and_interpolation(
    confidence, expected_tp_pct_ge, expected_tp_pct_le
):
    """Spot-check anchor points + interpolated midpoints."""
    pct = confidence_to_tp_pct(confidence)
    assert expected_tp_pct_ge <= pct <= expected_tp_pct_le


def test_buy_tp_above_entry_sl_below_entry():
    """A long trade: TP must be above entry, SL below."""
    lv = compute_tp_sl(
        side="buy",
        current_price=100.0,
        atr=1.0,
        confidence=2.0,  # ~8% TP
        target_rr=1.5,
    )
    assert lv.entry == 100.0
    assert lv.take_profit > lv.entry
    assert lv.stop_loss < lv.entry
    # TP should be at roughly entry + 8% (between 6% and 10% anchors).
    tp_dist = lv.take_profit - lv.entry
    assert 6.0 <= tp_dist <= 10.0


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
    """At normal ATR, the actual R:R equals the requested target_rr."""
    lv = compute_tp_sl(
        side="buy",
        current_price=100.0,
        atr=0.1,           # tiny ATR — floor won't bite
        confidence=2.0,    # ~8% TP → 8.0 absolute
        target_rr=2.0,
    )
    # tp_dist 8 / sl_dist 4 = 2.0 R:R
    assert pytest.approx(lv.rr, rel=0.01) == 2.0


def test_atr_floor_engages_on_low_volatility_pairs():
    """When the desired SL distance is smaller than 0.5×ATR, the floor
    kicks in and R:R drops below the target. We accept that — better to
    survive noise than to die on a tick."""
    lv = compute_tp_sl(
        side="buy",
        current_price=100.0,
        atr=10.0,         # huge ATR — floor will dominate
        confidence=2.0,
        target_rr=2.0,
        sl_floor_atr_mult=0.5,
    )
    assert lv.sl_atr_floor > 0  # floor engaged
    assert lv.rr < 2.0           # actual R:R smaller than target


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
    """The TPLevels.tp_pct field must equal the curve's value."""
    for conf in [0.2, 0.5, 1.0, 1.5, 2.0, 2.5, 3.5]:
        lv = compute_tp_sl("buy", 100.0, 1.0, confidence=conf, target_rr=1.5)
        assert lv.tp_pct == confidence_to_tp_pct(conf)
