"""Tests for FeedbackAdjuster — auto-tuning of confidence_threshold."""
from __future__ import annotations

import pytest

from app.strategies.feedback import THRESHOLD_FLOOR, FeedbackAdjuster


@pytest.fixture()
def adjuster(db_session, seed_bots):
    return FeedbackAdjuster(db=db_session, bot_id=seed_bots[0].id)


@pytest.mark.asyncio
async def test_warmup_when_total_trades_below_20(adjuster):
    stats = {"total_trades": 5, "win_rate": 0.5, "profit_factor": 1.2, "max_drawdown_pct": 1.0}
    new_t, action = await adjuster.adjust(stats, current_threshold=1.5)
    assert action == "warmup"
    assert new_t == 1.5  # unchanged


@pytest.mark.asyncio
async def test_tighten_when_win_rate_low(adjuster):
    stats = {"total_trades": 30, "win_rate": 0.40, "profit_factor": 1.0, "max_drawdown_pct": 2.0}
    new_t, action = await adjuster.adjust(stats, current_threshold=1.5)
    assert action == "tighten"
    assert new_t == pytest.approx(2.0)


@pytest.mark.asyncio
async def test_loosen_when_win_rate_and_pf_strong(adjuster):
    stats = {"total_trades": 30, "win_rate": 0.65, "profit_factor": 1.8, "max_drawdown_pct": 2.0}
    new_t, action = await adjuster.adjust(stats, current_threshold=1.5)
    assert action == "loosen"
    assert new_t == pytest.approx(1.3)


@pytest.mark.asyncio
async def test_loosen_never_below_floor(adjuster):
    stats = {"total_trades": 30, "win_rate": 0.65, "profit_factor": 1.8, "max_drawdown_pct": 2.0}
    new_t, action = await adjuster.adjust(stats, current_threshold=0.85)
    assert action == "loosen"
    assert new_t == pytest.approx(THRESHOLD_FLOOR)
    assert new_t >= 0.8


@pytest.mark.asyncio
async def test_pause_when_drawdown_exceeds_10pct(adjuster):
    stats = {"total_trades": 30, "win_rate": 0.55, "profit_factor": 1.2, "max_drawdown_pct": 12.5}
    new_t, action = await adjuster.adjust(stats, current_threshold=1.5)
    assert action == "pause"
    assert new_t == pytest.approx(2.5)


@pytest.mark.asyncio
async def test_hold_when_no_rule_triggers(adjuster):
    stats = {"total_trades": 30, "win_rate": 0.55, "profit_factor": 1.2, "max_drawdown_pct": 3.0}
    new_t, action = await adjuster.adjust(stats, current_threshold=1.5)
    assert action == "hold"
    assert new_t == 1.5


@pytest.mark.asyncio
async def test_pause_persists_paused_until_on_strategy_state(db_session, seed_bots):
    """When timeframe is provided AND action is 'pause', StrategyState gets paused_until."""
    from app.db.models import StrategyState

    state = StrategyState(
        bot_id=seed_bots[0].id,
        timeframe="1m",
        is_running=True,
        confidence_threshold=1.5,
    )
    db_session.add(state)
    db_session.commit()

    adj = FeedbackAdjuster(db=db_session, bot_id=seed_bots[0].id)
    stats = {"total_trades": 30, "win_rate": 0.55, "profit_factor": 1.2, "max_drawdown_pct": 12.5}
    new_t, action = await adj.adjust(stats, current_threshold=1.5, timeframe="1m")
    assert action == "pause"

    db_session.refresh(state)
    assert state.paused_until is not None
    assert state.is_running is False
    assert state.confidence_threshold == pytest.approx(2.5)
