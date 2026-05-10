"""Tests for the daily summary digest."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import patch, AsyncMock

import pytest

from app.db.models import TradeOutcome, Signal
import app.monitoring.daily_summary as ds


@pytest.fixture(autouse=True)
def reset_last_published():
    ds._last_published_day = None
    yield
    ds._last_published_day = None


@pytest.fixture
def patch_session_local(db_engine):
    from sqlalchemy.orm import sessionmaker
    TestSession = sessionmaker(bind=db_engine, autoflush=False, autocommit=False)
    with patch.object(ds, "SessionLocal", TestSession):
        yield


def _make_outcome(db_session, bot_id: int, *, pnl: float, when: datetime, r: float = 1.0):
    sig = Signal(bot_id=bot_id, instrument="BTCUSD", side="buy")
    db_session.add(sig)
    db_session.flush()
    o = TradeOutcome(
        bot_id=bot_id, signal_id=sig.id, instrument="BTCUSD", side="buy",
        timeframe="1m", entry_price=80000.0, exit_price=80100.0,
        qty=0.01, pnl_usd=pnl, r_multiple=r,
        opened_at=when - timedelta(minutes=5),
        closed_at=when,
        hold_seconds=300,
    )
    db_session.add(o)
    db_session.commit()


def test_compute_summary_no_trades(db_session, patch_session_local):
    start = datetime(2026, 5, 10, 0, 0, 0)
    end = datetime(2026, 5, 11, 0, 0, 0)
    s = ds.compute_summary(start, end)
    assert s["total_trades"] == 0
    assert s["total_pnl_usd"] == 0
    assert s["biggest_winner"] is None
    assert s["biggest_loser"] is None


def test_compute_summary_mixed_outcomes(db_session, seed_bots, patch_session_local):
    bot_id = seed_bots[0].id
    today_start = datetime(2026, 5, 10, 12, 0, 0)  # noon UTC, inside the day
    _make_outcome(db_session, bot_id, pnl=2.0, when=today_start, r=1.5)
    _make_outcome(db_session, bot_id, pnl=-1.0, when=today_start + timedelta(minutes=10), r=-0.5)
    _make_outcome(db_session, bot_id, pnl=0.5, when=today_start + timedelta(minutes=20), r=0.3)

    s = ds.compute_summary(
        datetime(2026, 5, 10, 0, 0, 0),
        datetime(2026, 5, 11, 0, 0, 0),
    )
    assert s["total_trades"] == 3
    assert s["wins"] == 2
    assert s["losses"] == 1
    assert s["total_pnl_usd"] == pytest.approx(1.5, abs=0.001)
    assert s["win_rate"] == pytest.approx(2 / 3, abs=0.001)
    assert s["biggest_winner"]["pnl_usd"] == pytest.approx(2.0)
    assert s["biggest_loser"]["pnl_usd"] == pytest.approx(-1.0)
    assert s["cumulative_r"] == pytest.approx(1.3, abs=0.01)


def test_compute_summary_max_drawdown(db_session, seed_bots, patch_session_local):
    """Cumulative path: +5, -3, +1, -2 → peak=5, trough=1, max DD = 4."""
    bot_id = seed_bots[0].id
    base = datetime(2026, 5, 10, 12, 0, 0)
    for pnl, delta in [(5.0, 0), (-3.0, 1), (1.0, 2), (-2.0, 3)]:
        _make_outcome(db_session, bot_id, pnl=pnl, when=base + timedelta(minutes=delta))
    s = ds.compute_summary(
        datetime(2026, 5, 10), datetime(2026, 5, 11),
    )
    assert s["max_drawdown_usd"] == pytest.approx(4.0, abs=0.001)


def test_compute_summary_skips_trades_outside_window(db_session, seed_bots, patch_session_local):
    bot_id = seed_bots[0].id
    # In-window
    _make_outcome(db_session, bot_id, pnl=1.0, when=datetime(2026, 5, 10, 12, 0))
    # Yesterday (outside)
    _make_outcome(db_session, bot_id, pnl=99.0, when=datetime(2026, 5, 9, 23, 30))
    # Tomorrow (outside)
    _make_outcome(db_session, bot_id, pnl=99.0, when=datetime(2026, 5, 11, 0, 1))
    s = ds.compute_summary(
        datetime(2026, 5, 10), datetime(2026, 5, 11),
    )
    assert s["total_trades"] == 1
    assert s["total_pnl_usd"] == pytest.approx(1.0)


@pytest.mark.asyncio
async def test_maybe_publish_idempotent(db_session, seed_bots, patch_session_local):
    """Called multiple times the same day → publishes once."""
    fake_post = AsyncMock(return_value=True)
    with patch.object(ds, "post_summary", new=fake_post):
        now = datetime(2026, 5, 10, 0, 5, 0)  # 00:05 UTC = just after midnight
        first = await ds.maybe_publish_daily_summary(now=now)
        second = await ds.maybe_publish_daily_summary(now=now)
    assert first is True
    assert second is False
    assert fake_post.call_count == 1


@pytest.mark.asyncio
async def test_maybe_publish_skips_late_in_day(db_session, patch_session_local):
    """If the process boots at noon, don't post yesterday's recap then."""
    fake_post = AsyncMock(return_value=True)
    with patch.object(ds, "post_summary", new=fake_post):
        now = datetime(2026, 5, 10, 14, 0, 0)  # 14:00 — well past the window
        result = await ds.maybe_publish_daily_summary(now=now)
    assert result is False
    fake_post.assert_not_called()


@pytest.mark.asyncio
async def test_post_summary_handles_no_webhook(db_session, patch_session_local):
    """If DISCORD_WEBHOOK_URL is not set, post_summary returns False cleanly."""
    with patch.dict("os.environ", {}, clear=False):
        # Force both env vars unset
        import os
        for k in ("DISCORD_WEBHOOK_URL", "DISCORD_SIGNALS_WEBHOOK_URL"):
            os.environ.pop(k, None)
        result = await ds.post_summary(
            datetime(2026, 5, 10), datetime(2026, 5, 11),
            {"total_trades": 0},
        )
        assert result is False
