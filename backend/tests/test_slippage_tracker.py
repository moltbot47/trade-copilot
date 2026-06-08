"""Tests for the slippage tracker — three-phase write cycle + slippage math.

Covers the contract the runners and partner pipeline depend on:

  - record_entry_signal creates a "pending" row with required fields
  - record_fill updates the row to "open" and computes entry slippage and
    latency components with the correct signed convention for buy / sell
  - finalize_record updates to "closed" and computes edge erosion
  - mark_rejected handles signals that never filled
  - list_records_for_user time-range filter works
  - compute_daily_summary aggregates correctly
"""
from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import patch

import pytest
from sqlalchemy.orm import sessionmaker

from app.db.models import SlippageRecord, User
from app.monitoring.slippage_tracker import (
    compute_daily_summary,
    count_pending_since,
    finalize_record,
    list_records_for_user,
    mark_rejected,
    record_entry_signal,
    record_fill,
)


@pytest.fixture(autouse=True)
def patch_session_local(db_engine):
    """Re-bind SessionLocal to the test engine so the helper functions —
    which open their own sessions — write into the in-memory test DB."""
    import app.monitoring.slippage_tracker as st

    TestSession = sessionmaker(bind=db_engine, autoflush=False, autocommit=False)
    with patch.object(st, "SessionLocal", TestSession):
        yield


@pytest.fixture
def user(db_session):
    u = User(email="audit@x.com", hashed_password="x")
    db_session.add(u)
    db_session.commit()
    return u


def _signal_kwargs(user_id: int, **overrides) -> dict:
    # bar_close_ts is set safely in the past relative to wall clock so
    # latency calcs (signal_ts - bar_close_ts) come out positive on any
    # test machine.
    base = dict(
        user_id=user_id,
        strategy_name="velocity_spike",
        account_id="2163244",
        symbol="NAS100",
        side="buy",
        bar_close_ts=datetime.utcnow() - timedelta(seconds=2),
        bar_close_price=29230.0,
        expected_entry_price=29230.0,
        hard_stop_distance_pts=50.0,
        trailing_stop_distance_pts=3.0,
        early_stop_condition="momentum_stalls_3_bars",
    )
    base.update(overrides)
    return base


# ----------------------------------------------------------------------- #
# record_entry_signal
# ----------------------------------------------------------------------- #
def test_record_entry_signal_creates_pending_row(user, db_session):
    rid = record_entry_signal(**_signal_kwargs(user.id))
    rec = db_session.get(SlippageRecord, rid)

    assert rec is not None
    assert rec.status == "pending"
    assert rec.symbol == "NAS100"
    assert rec.side == "buy"
    assert rec.expected_entry_price == 29230.0
    assert rec.hard_stop_distance_pts == 50.0
    assert rec.trailing_stop_distance_pts == 3.0
    assert rec.early_stop_condition == "momentum_stalls_3_bars"
    # signal_ts is set inside the helper to "now" — should be after bar_close
    assert rec.signal_ts >= rec.bar_close_ts
    # Entry-side fields not yet populated
    assert rec.actual_entry_price is None
    assert rec.entry_slippage_pts is None
    assert rec.fill_ts is None
    # Latency from bar close → signal emit
    assert rec.signal_latency_ms is not None and rec.signal_latency_ms >= 0


def test_record_entry_signal_persists_extra_json(user, db_session):
    rid = record_entry_signal(
        **_signal_kwargs(user.id, extra={"velocity": 1.23, "rsi": 72})
    )
    rec = db_session.get(SlippageRecord, rid)
    import json
    assert json.loads(rec.extra_json) == {"velocity": 1.23, "rsi": 72}


# ----------------------------------------------------------------------- #
# record_fill — slippage convention + latency components
# ----------------------------------------------------------------------- #
def test_record_fill_long_side_positive_slippage_when_filled_higher(user, db_session):
    rid = record_entry_signal(**_signal_kwargs(user.id))
    record_fill(rid, actual_entry_price=29230.4)
    db_session.expire_all()
    rec = db_session.get(SlippageRecord, rid)

    # Long: actual > expected → positive (worse than expected)
    assert rec.status == "open"
    assert rec.actual_entry_price == 29230.4
    assert rec.entry_slippage_pts == pytest.approx(0.4)
    assert rec.fill_ts is not None
    assert rec.total_latency_ms is not None and rec.total_latency_ms >= 0


def test_record_fill_short_side_positive_slippage_when_filled_lower(user, db_session):
    rid = record_entry_signal(**_signal_kwargs(user.id, side="sell"))
    record_fill(rid, actual_entry_price=29229.5)
    db_session.expire_all()
    rec = db_session.get(SlippageRecord, rid)

    # Short: expected > actual → positive (worse than expected — sold for less)
    assert rec.entry_slippage_pts == pytest.approx(0.5)


def test_record_fill_zero_when_filled_at_expected(user, db_session):
    rid = record_entry_signal(**_signal_kwargs(user.id))
    record_fill(rid, actual_entry_price=29230.0)
    db_session.expire_all()
    rec = db_session.get(SlippageRecord, rid)
    assert rec.entry_slippage_pts == pytest.approx(0.0)


def test_record_fill_computes_latency_components(user, db_session):
    rid = record_entry_signal(**_signal_kwargs(user.id))
    rec_before = db_session.get(SlippageRecord, rid)
    signal_ts = rec_before.signal_ts

    submit_ts = signal_ts + timedelta(milliseconds=50)
    ack_ts = submit_ts + timedelta(milliseconds=120)
    fill_ts = ack_ts + timedelta(milliseconds=80)
    record_fill(
        rid,
        actual_entry_price=29230.0,
        order_submit_ts=submit_ts,
        order_ack_ts=ack_ts,
        fill_ts=fill_ts,
    )
    db_session.expire_all()
    rec = db_session.get(SlippageRecord, rid)

    assert rec.submit_latency_ms == pytest.approx(50, abs=2)
    assert rec.broker_ack_latency_ms == pytest.approx(120, abs=2)
    assert rec.fill_latency_ms == pytest.approx(80, abs=2)


def test_record_fill_stores_broker_response_json(user, db_session):
    rid = record_entry_signal(**_signal_kwargs(user.id))
    broker_payload = {"orderId": "tl_8847291", "status": "filled", "qty": 0.01}
    record_fill(rid, actual_entry_price=29230.0, broker_response=broker_payload)
    db_session.expire_all()
    rec = db_session.get(SlippageRecord, rid)
    import json
    assert json.loads(rec.broker_fill_response_json) == broker_payload


def test_record_fill_missing_record_logs_warning_no_raise(user):
    # Should not raise even if id doesn't exist
    record_fill(999999, actual_entry_price=29230.0)


# ----------------------------------------------------------------------- #
# finalize_record — exit slippage + edge erosion
# ----------------------------------------------------------------------- #
def test_finalize_record_long_closes_with_real_and_strategy_pnl(user, db_session):
    rid = record_entry_signal(**_signal_kwargs(user.id))
    record_fill(rid, actual_entry_price=29230.5)  # slipped 0.5 worse
    finalize_record(
        rid,
        exit_type="trailing",
        actual_exit_price=29253.0,
        expected_exit_price=29254.0,  # strategy expected to exit higher
        peak_price=29256.0,
    )
    db_session.expire_all()
    rec = db_session.get(SlippageRecord, rid)

    assert rec.status == "closed"
    assert rec.exit_type == "trailing"
    assert rec.peak_price == 29256.0
    # Long exit_slippage: expected - actual → 29254 - 29253 = 1.0 (worse, exited lower)
    assert rec.exit_slippage_pts == pytest.approx(1.0)
    # Real P&L: actual_exit - actual_entry = 29253 - 29230.5 = 22.5
    assert rec.real_pnl_pts == pytest.approx(22.5)
    # Strategy P&L: expected_exit - expected_entry = 29254 - 29230 = 24
    assert rec.strategy_pnl_pts == pytest.approx(24.0)
    # Edge erosion: strategy - real = 1.5pts
    assert rec.slippage_total_pts == pytest.approx(1.5)


def test_finalize_record_short_closes_with_inverted_signs(user, db_session):
    rid = record_entry_signal(**_signal_kwargs(user.id, side="sell"))
    record_fill(rid, actual_entry_price=29229.5)  # short slipped 0.5 worse
    finalize_record(
        rid,
        exit_type="trailing",
        actual_exit_price=29220.0,
        expected_exit_price=29219.0,
    )
    db_session.expire_all()
    rec = db_session.get(SlippageRecord, rid)

    # Short exit_slippage: actual - expected → 29220 - 29219 = 1.0
    assert rec.exit_slippage_pts == pytest.approx(1.0)
    # Real P&L (short): entry - exit = 29229.5 - 29220 = 9.5
    assert rec.real_pnl_pts == pytest.approx(9.5)
    # Strategy P&L: 29230 - 29219 = 11.0
    assert rec.strategy_pnl_pts == pytest.approx(11.0)
    # Edge erosion: 11 - 9.5 = 1.5
    assert rec.slippage_total_pts == pytest.approx(1.5)


def test_finalize_record_uses_point_value_for_dollar_friction(user, db_session):
    rid = record_entry_signal(**_signal_kwargs(user.id))
    record_fill(rid, actual_entry_price=29230.0)
    finalize_record(
        rid,
        exit_type="trailing",
        actual_exit_price=29233.0,
        expected_exit_price=29235.0,
        point_value_usd=0.10,  # 0.01 lot Nas100 CFD
    )
    db_session.expire_all()
    rec = db_session.get(SlippageRecord, rid)

    # 2pt edge erosion × $0.10/pt = $0.20
    assert rec.slippage_total_dollars == pytest.approx(0.20)


def test_finalize_record_without_expected_exit_skips_strategy_pnl(user, db_session):
    rid = record_entry_signal(**_signal_kwargs(user.id))
    record_fill(rid, actual_entry_price=29230.0)
    finalize_record(rid, exit_type="manual", actual_exit_price=29235.0)
    db_session.expire_all()
    rec = db_session.get(SlippageRecord, rid)

    assert rec.real_pnl_pts == pytest.approx(5.0)
    assert rec.strategy_pnl_pts is None
    assert rec.slippage_total_pts is None


# ----------------------------------------------------------------------- #
# rejected lifecycle path
# ----------------------------------------------------------------------- #
def test_mark_rejected_sets_status(user, db_session):
    rid = record_entry_signal(**_signal_kwargs(user.id))
    mark_rejected(rid)
    db_session.expire_all()
    rec = db_session.get(SlippageRecord, rid)
    assert rec.status == "rejected"


# ----------------------------------------------------------------------- #
# list_records_for_user
# ----------------------------------------------------------------------- #
def test_list_records_filters_by_user(user, db_session):
    other = User(email="other@x.com", hashed_password="x")
    db_session.add(other)
    db_session.commit()

    record_entry_signal(**_signal_kwargs(user.id, symbol="NAS100"))
    record_entry_signal(**_signal_kwargs(other.id, symbol="EURUSD"))

    rows = list_records_for_user(user.id)
    assert len(rows) == 1
    assert rows[0].symbol == "NAS100"


def test_list_records_filters_by_strategy_and_account(user, db_session):
    record_entry_signal(**_signal_kwargs(user.id, strategy_name="velocity_spike"))
    record_entry_signal(**_signal_kwargs(user.id, strategy_name="vwap_reversion"))

    rows = list_records_for_user(user.id, strategy_name="velocity_spike")
    assert len(rows) == 1
    assert rows[0].strategy_name == "velocity_spike"


def test_list_records_respects_status_filter(user, db_session):
    rid_open = record_entry_signal(**_signal_kwargs(user.id))
    record_fill(rid_open, actual_entry_price=29230.0)
    record_entry_signal(**_signal_kwargs(user.id))  # stays pending

    rows = list_records_for_user(user.id, status="open")
    assert len(rows) == 1
    assert rows[0].status == "open"


# ----------------------------------------------------------------------- #
# compute_daily_summary
# ----------------------------------------------------------------------- #
def test_compute_daily_summary_aggregates_closed_trades(user, db_session):
    today = datetime.utcnow()
    # Two closed trades + one rejected + one still open
    for actual_entry, expected_exit, actual_exit in [
        (29230.5, 29254.0, 29253.0),  # erosion 1.5 (entry .5 + exit 1.0)
        (29231.0, 29260.0, 29258.0),  # erosion 3.0 (entry 1.0 + exit 2.0)
    ]:
        rid = record_entry_signal(**_signal_kwargs(user.id))
        record_fill(rid, actual_entry_price=actual_entry)
        finalize_record(
            rid,
            exit_type="trailing",
            actual_exit_price=actual_exit,
            expected_exit_price=expected_exit,
        )

    rid_rej = record_entry_signal(**_signal_kwargs(user.id))
    mark_rejected(rid_rej)
    rid_open = record_entry_signal(**_signal_kwargs(user.id))
    record_fill(rid_open, actual_entry_price=29230.0)

    summary = compute_daily_summary(user.id, day=today)

    assert summary["signals_emitted"] == 4
    assert summary["signals_rejected"] == 1
    assert summary["trades_closed"] == 2
    assert summary["trades_open"] == 1
    # Strategy PnL totals: 24 + 30 = 54;  Real: 22.5 + 27 = 49.5;  erosion 4.5
    assert summary["strategy_pnl_pts"] == pytest.approx(54.0)
    assert summary["real_pnl_pts"] == pytest.approx(49.5)
    assert summary["edge_erosion_pts"] == pytest.approx(4.5)
    # Entry slippages across all 4 records w/ entry filled: 0.5, 1.0, 0.0
    # (rejected has no fill, so its entry_slippage stays None)
    assert summary["worst_entry_slippage_pts"] == pytest.approx(1.0)


def test_compute_daily_summary_zero_when_no_records(user):
    summary = compute_daily_summary(user.id)
    assert summary["signals_emitted"] == 0
    assert summary["trades_closed"] == 0
    assert summary["strategy_pnl_pts"] == 0.0


# ----------------------------------------------------------------------- #
# count_pending_since — health check helper
# ----------------------------------------------------------------------- #
def test_count_pending_since_counts_old_pending_only(user, db_session):
    rid_old = record_entry_signal(**_signal_kwargs(user.id))
    # Force signal_ts into the past so the "stale" filter catches it
    rec = db_session.get(SlippageRecord, rid_old)
    rec.signal_ts = datetime.utcnow() - timedelta(minutes=10)
    db_session.commit()

    # A fresh pending should NOT be counted
    record_entry_signal(**_signal_kwargs(user.id))

    cutoff = datetime.utcnow() - timedelta(minutes=5)
    stale = count_pending_since(user.id, since=cutoff)
    assert stale == 1
