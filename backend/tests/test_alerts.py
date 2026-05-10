"""Tests for the health-alerting layer."""
from __future__ import annotations

import pytest

from app.monitoring.alerts import (
    HealthAlerter,
    SYNTHETIC_STREAK_THRESHOLD,
    get_alerter,
)


@pytest.fixture(autouse=True)
def reset_singleton():
    get_alerter().reset()
    yield
    get_alerter().reset()


def test_no_alert_below_threshold():
    a = HealthAlerter()
    for _ in range(SYNTHETIC_STREAK_THRESHOLD - 1):
        assert a.record("synthetic", failure=True) is None


def test_alert_fires_at_threshold():
    a = HealthAlerter()
    msg = None
    for _ in range(SYNTHETIC_STREAK_THRESHOLD):
        msg = a.record("synthetic", failure=True)
    assert msg is not None
    assert "ALERT" in msg
    assert "synthetic" in msg


def test_alert_cooldown_prevents_flood():
    a = HealthAlerter()
    # First alert fires
    for _ in range(SYNTHETIC_STREAK_THRESHOLD):
        first = a.record("synthetic", failure=True)
    assert first is not None
    # Further failures within cooldown: no new alert
    for _ in range(10):
        assert a.record("synthetic", failure=True) is None


def test_recovery_message_after_alert():
    a = HealthAlerter()
    for _ in range(SYNTHETIC_STREAK_THRESHOLD):
        a.record("synthetic", failure=True)
    # Now a success — should fire recovery
    msg = a.record("synthetic", failure=False)
    assert msg is not None
    assert "RECOVERED" in msg


def test_recovery_only_after_previous_alert():
    a = HealthAlerter()
    # A short streak of failures (below threshold) then recovery
    for _ in range(2):
        a.record("synthetic", failure=True)
    # No alert fired, so no recovery message either
    assert a.record("synthetic", failure=False) is None


def test_independent_streams():
    a = HealthAlerter()
    # Build up synthetic streak
    for _ in range(SYNTHETIC_STREAK_THRESHOLD - 1):
        a.record("synthetic", failure=True)
    # token_refresh stream is unaffected
    for _ in range(SYNTHETIC_STREAK_THRESHOLD - 1):
        assert a.record("token_refresh", failure=True) is None
    # Final tick on synthetic stream fires
    assert a.record("synthetic", failure=True) is not None
    # token_refresh still hasn't tripped
    assert a.record("token_refresh", failure=True) is not None  # this one tips over now


def test_singleton_is_stable():
    a1 = get_alerter()
    a2 = get_alerter()
    assert a1 is a2
