"""Unit tests for StrategyRunner orchestration."""
from __future__ import annotations

import asyncio
from unittest.mock import patch

import pytest

from app.strategies.runner import StrategyRunner, get_runner, tf_seconds


def _session_factory(db_session):
    """Wrap a sqlalchemy session so the factory returns a session that DOES NOT
    actually close (we want the test session to persist for assertions)."""
    class _Wrapper:
        def __init__(self, sess):
            self._s = sess

        def __getattr__(self, item):
            return getattr(self._s, item)

        def close(self):  # ignore close calls inside runner
            pass

    def _factory():
        return _Wrapper(db_session)

    return _factory


def test_tf_seconds_minute_format():
    assert tf_seconds("1m") == 60
    assert tf_seconds("5m") == 300


def test_tf_seconds_hour_format():
    assert tf_seconds("1h") == 3600


def test_tf_seconds_unknown_format_returns_default():
    assert tf_seconds("weird") == 60


def test_runner_can_be_instantiated(db_session, seed_bots):
    factory = _session_factory(db_session)
    runner = StrategyRunner(
        db_session_factory=factory,
        bot_id=seed_bots[0].id,
        timeframe="1m",
        symbols=["BTCUSD"],
        user_emails=["x@example.com"],
    )
    assert runner.bot_id == seed_bots[0].id
    assert runner.timeframe == "1m"
    assert runner.symbols == ["BTCUSD"]
    assert runner.task is None


def test_get_runner_returns_none_when_not_started(seed_bots):
    # Some other test may have started a runner; pick a tf that isn't used.
    bot_id = seed_bots[0].id
    assert get_runner(bot_id, "99m") is None


@pytest.mark.asyncio
async def test_start_creates_asyncio_task(db_session, seed_bots, monkeypatch):
    """Start the runner with run_loop stubbed out so it doesn't try to hit
    TradeLocker / DB beyond the very first lifecycle calls."""
    factory = _session_factory(db_session)
    bot_id = seed_bots[0].id

    async def _noop_run_loop(self):
        # Sleep so we have a live, cancelable task
        await asyncio.sleep(60)

    with patch.object(StrategyRunner, "run_loop", _noop_run_loop):
        runner = await StrategyRunner.start(
            db_session_factory=factory,
            bot_id=bot_id,
            timeframe="77m",
            symbols=["BTCUSD"],
            user_emails=["x@example.com"],
        )
        assert runner.task is not None
        assert not runner.task.done()
        assert get_runner(bot_id, "77m") is runner

        await runner.stop()
        assert get_runner(bot_id, "77m") is None


@pytest.mark.asyncio
async def test_start_returns_existing_runner_if_running(db_session, seed_bots):
    factory = _session_factory(db_session)
    bot_id = seed_bots[0].id

    async def _sleepy(self):
        await asyncio.sleep(60)

    with patch.object(StrategyRunner, "run_loop", _sleepy):
        first = await StrategyRunner.start(
            db_session_factory=factory,
            bot_id=bot_id,
            timeframe="78m",
            symbols=[],
            user_emails=[],
        )
        second = await StrategyRunner.start(
            db_session_factory=factory,
            bot_id=bot_id,
            timeframe="78m",
            symbols=[],
            user_emails=[],
        )
        assert first is second
        await first.stop()


@pytest.mark.asyncio
async def test_record_error_persists_to_strategy_state(db_session, seed_bots):
    """Verify _record_error writes last_error onto a StrategyState row."""
    from app.db.models import StrategyState
    factory = _session_factory(db_session)
    bot_id = seed_bots[0].id
    state = StrategyState(bot_id=bot_id, timeframe="1m", is_running=False)
    db_session.add(state)
    db_session.commit()

    runner = StrategyRunner(
        db_session_factory=factory,
        bot_id=bot_id,
        timeframe="1m",
        symbols=[],
        user_emails=[],
    )
    runner._record_error("synthetic failure")
    db_session.refresh(state)
    assert state.last_error == "synthetic failure"


@pytest.mark.asyncio
async def test_runner_persists_running_state_on_init(db_session, seed_bots):
    """_init_state should INSERT a StrategyState row with is_running=True."""
    from app.db.models import StrategyState

    factory = _session_factory(db_session)
    bot_id = seed_bots[1].id
    runner = StrategyRunner(
        db_session_factory=factory,
        bot_id=bot_id,
        timeframe="5m",
        symbols=[],
        user_emails=[],
    )
    await runner._init_state()

    state = (
        db_session.query(StrategyState)
        .filter(StrategyState.bot_id == bot_id, StrategyState.timeframe == "5m")
        .first()
    )
    assert state is not None
    assert state.is_running is True


def test_current_threshold_default_when_no_state(db_session, seed_bots):
    factory = _session_factory(db_session)
    runner = StrategyRunner(
        db_session_factory=factory,
        bot_id=seed_bots[2].id,
        timeframe="1m",
        symbols=[],
        user_emails=[],
    )
    # No StrategyState row → default 1.5
    assert runner._current_threshold() == pytest.approx(1.5)
