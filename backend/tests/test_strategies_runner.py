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


# ============================================================================
# Kill-switch guardrail tests (added 2026-05-10 after CRITICAL #1 regression).
# These exercise _check_live_guardrails directly so the camelCase key contract
# with TradeLockerClient.get_account_state can't silently break again.
# ============================================================================

from app.strategies.runner import QuantRunner
from app.db.models import User


def _make_quant_runner(db_session, seed_bots):
    factory = _session_factory(db_session)
    return QuantRunner(
        db_session_factory=factory,
        bot_id=seed_bots[2].id,
        timeframe="1m",
        symbols=["BTCUSD"],
        user_emails=["x@example.com"],
    )


def _make_user(env="live", kill_pct=20.0):
    u = User(email="kt@example.com", hashed_password="x")
    u.tradelocker_env = env
    u.daily_kill_switch_pct = kill_pct
    return u


def test_kill_switch_passes_when_env_is_demo(db_session, seed_bots):
    runner = _make_quant_runner(db_session, seed_bots)
    user = _make_user(env="demo", kill_pct=20.0)
    state = {"balance": 100.0, "todayNet": -50.0}  # would otherwise breach
    allowed, reason = runner._check_live_guardrails(user, state)
    assert allowed is True
    assert reason is None


def test_kill_switch_passes_when_kill_pct_is_zero(db_session, seed_bots):
    runner = _make_quant_runner(db_session, seed_bots)
    user = _make_user(kill_pct=0)
    state = {"balance": 100.0, "todayNet": -99.0}
    allowed, reason = runner._check_live_guardrails(user, state)
    assert allowed is True


def test_kill_switch_uses_camelcase_todayNet_key(db_session, seed_bots):
    """Regression test for CRITICAL bug 2026-05-10: runner previously read
    'today_net' (snake) but TradeLockerClient returns 'todayNet' (camel).
    """
    runner = _make_quant_runner(db_session, seed_bots)
    user = _make_user(kill_pct=20.0)
    state = {"balance": 100.0, "todayNet": -25.0}  # 25% loss > 20% cap
    allowed, reason = runner._check_live_guardrails(user, state)
    assert allowed is False
    assert "daily_kill_switch_hit" in (reason or "")


def test_kill_switch_does_NOT_match_snake_key(db_session, seed_bots):
    """If state has snake_case key only (typo regression), kill switch
    should still fail-soft to True — but should NOT fire on imaginary data."""
    runner = _make_quant_runner(db_session, seed_bots)
    user = _make_user(kill_pct=20.0)
    state = {"balance": 100.0, "today_net": -25.0}  # wrong key
    allowed, _ = runner._check_live_guardrails(user, state)
    # Fail-soft: missing key → allow (other guardrails still apply)
    assert allowed is True


def test_kill_switch_fires_at_exact_threshold(db_session, seed_bots):
    runner = _make_quant_runner(db_session, seed_bots)
    user = _make_user(kill_pct=10.0)
    state = {"balance": 1000.0, "todayNet": -100.0}  # exactly 10%
    allowed, reason = runner._check_live_guardrails(user, state)
    assert allowed is False
    assert "10.00%" in (reason or "") or "10.0%" in (reason or "")


def test_kill_switch_allows_when_profitable(db_session, seed_bots):
    runner = _make_quant_runner(db_session, seed_bots)
    user = _make_user(kill_pct=20.0)
    state = {"balance": 1000.0, "todayNet": 50.0}  # profit
    allowed, reason = runner._check_live_guardrails(user, state)
    assert allowed is True


def test_kill_switch_fail_soft_on_empty_state(db_session, seed_bots):
    """Broker hiccup: account_state is None → allow rather than halt
    (lot cap + position cap still bound risk)."""
    runner = _make_quant_runner(db_session, seed_bots)
    user = _make_user(kill_pct=20.0)
    allowed, _ = runner._check_live_guardrails(user, None)
    assert allowed is True
    allowed, _ = runner._check_live_guardrails(user, {})
    assert allowed is True


# ============================================================================
# Circuit breaker tests (P2 #92) — auto-halt after N consecutive losses
# ============================================================================

from datetime import datetime, timedelta
from app.db.models import TradeOutcome, Signal, Execution, ExecutionStatus


def _make_loss_outcome(db_session, bot_id, user_id, pnl=-5.0, signal_id=None):
    """Helper: create a TradeOutcome with a corresponding signal+execution
    (the circuit breaker joins through Execution to find user-specific
    trades)."""
    if signal_id is None:
        sig = Signal(bot_id=bot_id, instrument="BTCUSD", side="buy")
        db_session.add(sig)
        db_session.flush()
        signal_id = sig.id
        execu = Execution(
            signal_id=signal_id,
            user_id=user_id,
            status=ExecutionStatus.filled,
            executed_lot_size=0.01,
        )
        db_session.add(execu)
        db_session.flush()
    outcome = TradeOutcome(
        bot_id=bot_id,
        signal_id=signal_id,
        instrument="BTCUSD",
        side="buy",
        timeframe="1m",
        entry_price=80000.0,
        exit_price=79900.0 if pnl < 0 else 80100.0,
        qty=0.01,
        pnl_usd=pnl,
        r_multiple=-1.0 if pnl < 0 else 1.0,
        opened_at=datetime.utcnow(),
        closed_at=datetime.utcnow(),
        hold_seconds=60,
    )
    db_session.add(outcome)
    db_session.flush()
    return outcome


def test_circuit_breaker_disabled_when_n_zero(db_session, seed_bots):
    runner = _make_quant_runner(db_session, seed_bots)
    user = _make_user(env="live")
    user.circuit_breaker_n_losses = None
    allowed, reason = runner._check_circuit_breaker(user)
    assert allowed is True
    assert reason is None


def test_circuit_breaker_passes_with_no_history(db_session, seed_bots):
    """No closed trades yet → breaker has nothing to evaluate."""
    runner = _make_quant_runner(db_session, seed_bots)
    user = User(email="cb-test@example.com", hashed_password="x"); user.id=1; db_session.add(user); db_session.flush()
    user.circuit_breaker_n_losses = 3
    db_session.commit()
    allowed, _ = runner._check_circuit_breaker(user)
    assert allowed is True


def test_circuit_breaker_trips_after_n_losses(db_session, seed_bots):
    runner = _make_quant_runner(db_session, seed_bots)
    user = User(email="cb-test@example.com", hashed_password="x"); user.id=1; db_session.add(user); db_session.flush()
    user.circuit_breaker_n_losses = 3
    user.circuit_breaker_cooldown_minutes = 60
    db_session.commit()

    bot_id = seed_bots[2].id
    runner.bot_id = bot_id
    for _ in range(3):
        _make_loss_outcome(db_session, bot_id, user.id, pnl=-5.0)
    db_session.commit()

    allowed, reason = runner._check_circuit_breaker(user)
    assert allowed is False
    assert "circuit_breaker_tripped" in (reason or "")
    db_session.refresh(user)
    assert user.circuit_breaker_tripped_at is not None


def test_circuit_breaker_does_not_trip_with_mixed_results(db_session, seed_bots):
    runner = _make_quant_runner(db_session, seed_bots)
    user = User(email="cb-test@example.com", hashed_password="x"); user.id=1; db_session.add(user); db_session.flush()
    user.circuit_breaker_n_losses = 3
    db_session.commit()

    bot_id = seed_bots[2].id
    runner.bot_id = bot_id
    _make_loss_outcome(db_session, bot_id, user.id, pnl=-5.0)
    _make_loss_outcome(db_session, bot_id, user.id, pnl=10.0)  # winner
    _make_loss_outcome(db_session, bot_id, user.id, pnl=-5.0)
    db_session.commit()

    allowed, _ = runner._check_circuit_breaker(user)
    assert allowed is True


def test_circuit_breaker_still_cooling_blocks_entries(db_session, seed_bots):
    runner = _make_quant_runner(db_session, seed_bots)
    user = User(email="cb-test@example.com", hashed_password="x"); user.id=1; db_session.add(user); db_session.flush()
    user.circuit_breaker_n_losses = 3
    user.circuit_breaker_cooldown_minutes = 60
    user.circuit_breaker_tripped_at = datetime.utcnow() - timedelta(minutes=10)  # tripped 10 min ago
    db_session.commit()

    allowed, reason = runner._check_circuit_breaker(user)
    assert allowed is False
    assert "cooling" in (reason or "")


def test_circuit_breaker_clears_after_cooldown(db_session, seed_bots):
    runner = _make_quant_runner(db_session, seed_bots)
    user = User(email="cb-test@example.com", hashed_password="x"); user.id=1; db_session.add(user); db_session.flush()
    user.circuit_breaker_n_losses = 3
    user.circuit_breaker_cooldown_minutes = 60
    user.circuit_breaker_tripped_at = datetime.utcnow() - timedelta(minutes=120)  # cooled
    db_session.commit()

    allowed, _ = runner._check_circuit_breaker(user)
    # No recent losses in DB, so breaker stays cleared
    assert allowed is True
    db_session.refresh(user)
    assert user.circuit_breaker_tripped_at is None
