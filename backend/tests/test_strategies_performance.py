"""Tests for PerformanceTracker — record_trade_close, rolling stats, snapshots."""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from app.db.models import PerformanceSnapshot, TradeOutcome
from app.strategies.performance_tracker import PerformanceTracker


@pytest.fixture()
def tracker(db_session, seed_bots):
    return PerformanceTracker(db=db_session, bot_id=seed_bots[0].id)


def _make_outcome(
    bot_id: int,
    closed_at: datetime,
    pnl_usd: float,
    r_multiple: float,
    instrument: str = "BTCUSD",
    side: str = "buy",
) -> TradeOutcome:
    return TradeOutcome(
        bot_id=bot_id,
        instrument=instrument,
        side=side,
        timeframe="1m",
        entry_price=100.0,
        exit_price=100.0 + pnl_usd / 0.01,
        qty=0.01,
        pnl_usd=pnl_usd,
        r_multiple=r_multiple,
        opened_at=closed_at - timedelta(minutes=30),
        closed_at=closed_at,
        hold_seconds=1800,
    )


@pytest.mark.asyncio
async def test_record_trade_close_persists_row(tracker, db_session, seed_bots):
    closed_at = datetime.utcnow()
    outcome = _make_outcome(seed_bots[0].id, closed_at, pnl_usd=10.0, r_multiple=1.0)
    await tracker.record_trade_close(outcome)
    rows = db_session.query(TradeOutcome).filter(TradeOutcome.bot_id == seed_bots[0].id).all()
    assert len(rows) == 1
    assert rows[0].pnl_usd == 10.0


def test_rolling_stats_with_no_trades_returns_defaults(tracker):
    stats = tracker.compute_rolling_stats(window=20)
    assert stats["total_trades"] == 0
    assert stats["win_rate"] == 0.0
    assert stats["profit_factor"] == 0.0
    assert stats["window_size"] == 20


def test_rolling_stats_with_20_trades(tracker, db_session, seed_bots):
    """Build a known sequence: 12 wins of +1R, 8 losses of -1R → win_rate=0.6, PF=1.5."""
    base = datetime.utcnow()
    for i in range(12):
        db_session.add(_make_outcome(seed_bots[0].id, base + timedelta(seconds=i),
                                     pnl_usd=10.0, r_multiple=1.0))
    for i in range(12, 20):
        db_session.add(_make_outcome(seed_bots[0].id, base + timedelta(seconds=i),
                                     pnl_usd=-10.0, r_multiple=-1.0))
    db_session.commit()

    stats = tracker.compute_rolling_stats(window=20)
    assert stats["total_trades"] == 20
    assert stats["win_rate"] == pytest.approx(0.6, abs=0.01)
    # 12 wins * 10 / (8 losses * 10) = 120 / 80 = 1.5
    assert stats["profit_factor"] == pytest.approx(1.5, abs=0.01)
    assert stats["total_pnl_usd"] == pytest.approx(120.0 - 80.0)
    # max_dd_pct should be a finite, non-negative number.
    assert stats["max_drawdown_pct"] >= 0.0
    assert stats["sharpe"] != 0.0


def test_rolling_stats_all_wins(tracker, db_session, seed_bots):
    """When there are no losses, profit_factor must be capped (not inf)."""
    base = datetime.utcnow()
    for i in range(5):
        db_session.add(_make_outcome(seed_bots[0].id, base + timedelta(seconds=i),
                                     pnl_usd=5.0, r_multiple=0.5))
    db_session.commit()

    stats = tracker.compute_rolling_stats(window=10)
    assert stats["total_trades"] == 5
    assert stats["win_rate"] == 1.0
    assert stats["profit_factor"] == 99.0  # capped


def test_write_snapshot_creates_row(tracker, db_session, seed_bots):
    stats = {
        "window_size": 20,
        "total_trades": 20,
        "win_rate": 0.55,
        "profit_factor": 1.4,
        "sharpe": 0.6,
        "avg_r": 0.3,
        "max_drawdown_pct": 4.0,
        "total_pnl_usd": 250.0,
    }
    snap = tracker.write_snapshot(stats, threshold_after=1.7, feedback_action="hold")
    assert snap.id is not None

    rows = (
        db_session.query(PerformanceSnapshot)
        .filter(PerformanceSnapshot.bot_id == seed_bots[0].id)
        .all()
    )
    assert len(rows) == 1
    s = rows[0]
    assert s.win_rate == pytest.approx(0.55)
    assert s.profit_factor == pytest.approx(1.4)
    assert s.threshold_after == pytest.approx(1.7)
    assert s.feedback_action == "hold"
    assert s.total_trades == 20


def test_total_outcomes_counts_correctly(tracker, db_session, seed_bots):
    base = datetime.utcnow()
    for i in range(7):
        db_session.add(_make_outcome(seed_bots[0].id, base + timedelta(seconds=i),
                                     pnl_usd=1.0, r_multiple=0.1))
    db_session.commit()
    assert tracker.total_outcomes() == 7


def test_latest_snapshot_returns_most_recent(tracker, db_session, seed_bots):
    older = PerformanceSnapshot(
        bot_id=seed_bots[0].id,
        snapshot_at=datetime.utcnow() - timedelta(hours=1),
        window_size=20,
        feedback_action="hold",
    )
    newer = PerformanceSnapshot(
        bot_id=seed_bots[0].id,
        snapshot_at=datetime.utcnow(),
        window_size=20,
        feedback_action="loosen",
    )
    db_session.add_all([older, newer])
    db_session.commit()

    latest = tracker.latest_snapshot()
    assert latest is not None
    assert latest.feedback_action == "loosen"
