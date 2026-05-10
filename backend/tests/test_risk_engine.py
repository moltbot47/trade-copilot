"""Risk engine unit tests — pure math, no I/O."""
from __future__ import annotations

import pytest

from app.core.risk_engine import _aggression_multiplier, compute_user_lot


# ---- _aggression_multiplier ----

def test_aggression_1_is_quarter_x():
    assert _aggression_multiplier(1) == pytest.approx(0.25)


def test_aggression_5_is_one_x():
    assert _aggression_multiplier(5) == pytest.approx(1.0)


def test_aggression_10_is_two_x():
    assert _aggression_multiplier(10) == pytest.approx(2.0)


def test_aggression_below_min_clamps_to_1():
    assert _aggression_multiplier(0) == pytest.approx(0.25)
    assert _aggression_multiplier(-99) == pytest.approx(0.25)


def test_aggression_above_max_clamps_to_10():
    assert _aggression_multiplier(99) == pytest.approx(2.0)


def test_aggression_monotonic():
    last = -1.0
    for a in range(1, 11):
        m = _aggression_multiplier(a)
        assert m >= last
        last = m


# ---- compute_user_lot ----

def test_compute_user_lot_aggression_1_quarter():
    """At aggression 1, lot = 0.25 × base, rounded to step."""
    lot = compute_user_lot(base_lot=1.0, aggression_level=1, account_balance=10_000.0)
    assert lot == pytest.approx(0.25, abs=0.01)


def test_compute_user_lot_aggression_5_one_x():
    lot = compute_user_lot(base_lot=1.0, aggression_level=5, account_balance=10_000.0)
    # capped at max_risk_pct (3.0), so 1.0 base * 1.0 mult = 1.0 → not capped
    assert lot == pytest.approx(1.0, abs=0.01)


def test_compute_user_lot_aggression_10_two_x_but_capped():
    """Aggression 10 → 2.0× base, but capped at max_risk_pct (default 3.0)."""
    lot = compute_user_lot(base_lot=1.0, aggression_level=10, account_balance=10_000.0)
    # 1.0 × 2.0 = 2.0, < 3.0 cap, so result = 2.0
    assert lot == pytest.approx(2.0, abs=0.01)


def test_compute_user_lot_zero_base_returns_zero():
    assert compute_user_lot(base_lot=0.0, aggression_level=5, account_balance=10_000.0) == 0.0


def test_compute_user_lot_negative_base_returns_zero():
    assert compute_user_lot(base_lot=-1.0, aggression_level=5, account_balance=10_000.0) == 0.0


def test_compute_user_lot_max_risk_caps_lot():
    """When per_lot_risk_usd is provided, cap = balance × risk% / per_lot_risk."""
    # $10k balance × 3% = $300 max risk; $100/lot risk → cap = 3.0 lots.
    lot = compute_user_lot(
        base_lot=5.0,
        aggression_level=10,
        account_balance=10_000.0,
        max_risk_pct=3.0,
        per_lot_risk_usd=100.0,
    )
    assert lot == pytest.approx(3.0, abs=0.01)


def test_compute_user_lot_max_risk_caps_lot_low():
    # $10k × 1% = $100; $100/lot → cap = 1.0 lot.
    lot = compute_user_lot(
        base_lot=5.0,
        aggression_level=10,
        account_balance=10_000.0,
        max_risk_pct=1.0,
        per_lot_risk_usd=100.0,
    )
    assert lot == pytest.approx(1.0, abs=0.01)


def test_compute_user_lot_no_per_lot_risk_skips_balance_cap():
    """Without per_lot_risk_usd, the risk-based cap is disabled (logged
    at DEBUG). Lot is only bounded by aggression × base, step, and max_lot_cap.
    Regression check: previously 'capped = max_risk_pct' silently treated
    a percentage as a lot count, which under-capped tiny accounts and
    over-capped large ones. New behavior: no cap unless caller knows risk.
    """
    lot = compute_user_lot(
        base_lot=5.0, aggression_level=10, account_balance=10_000.0, max_risk_pct=3.0,
    )
    # 5.0 × 2.0 mult = 10.0; no balance cap applies → returns 10.0
    assert lot == pytest.approx(10.0, abs=0.01)


def test_compute_user_lot_step_rounding():
    """Result rounds DOWN to broker step increment."""
    lot = compute_user_lot(
        base_lot=0.337,
        aggression_level=5,
        account_balance=10_000.0,
        step=0.01,
    )
    # 0.337 * 1.0 = 0.337 → round down to 0.33 (with step 0.01)
    assert lot == pytest.approx(0.33, abs=0.01)


def test_compute_user_lot_invalid_step_falls_back():
    lot = compute_user_lot(
        base_lot=0.5, aggression_level=5, account_balance=10_000.0, step=0.0
    )
    assert lot > 0
