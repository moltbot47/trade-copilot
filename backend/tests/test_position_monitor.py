"""Unit tests for PositionMonitor — watches open positions, records outcomes.

Before these tests, position_monitor.py was 15% covered (100 of 118 stmts
missing). Cover the high-value paths:

  - track() registers MAE/MFE state
  - poll_and_close() returns [] when nothing tracked
  - Still-open positions update MAE/MFE (high/low water marks)
  - Closed positions (qty=0 / missing) build TradeOutcome rows
  - Instrument-specific lot multipliers (FX, crypto, metals, JPY)
  - R-multiple uses |entry - SL|
  - Skipping when broker token decrypt fails / no creds
  - Signal lookup miss returns None
  - Forecast extraction from raw_payload JSON
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.core.crypto import encrypt
from app.core.tradelocker_client import TradeLockerError
from app.db.models import (
    Execution,
    ExecutionStatus,
    Signal,
    StrategyType,
    TradeOutcome,
    User,
)
from app.strategies.position_monitor import PositionMonitor, _extract_forecast


# ---------------- helpers ----------------

def _session_factory(db_session):
    """Returns a callable that yields the test session, swallowing close()."""

    class _Wrap:
        def __init__(self, s):
            self._s = s

        def __getattr__(self, item):
            return getattr(self._s, item)

        def close(self):
            pass

    def _factory():
        return _Wrap(db_session)

    return _factory


def _make_user(db_session, *, connected: bool = True) -> User:
    user = User(email=f"pm-{datetime.utcnow().timestamp()}@example.com", hashed_password="x")
    if connected:
        user.tradelocker_account_id = "ACC-1"
        user.tradelocker_acc_num = "1"
        user.tradelocker_env = "demo"
        user.tradelocker_token = encrypt("tok")
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user


def _make_signal(db_session, bot_id: int, **overrides) -> Signal:
    defaults = dict(
        bot_id=bot_id,
        instrument="EURUSD",
        side="buy",
        entry_price=1.1000,
        stop_loss=1.0950,
        take_profit=1.1100,
        base_lot_size=0.10,
        raw_payload=json.dumps(
            {"forecast_drift": 1.5, "forecast_confidence": 2.0, "threshold": 0.5}
        ),
    )
    defaults.update(overrides)
    sig = Signal(**defaults)
    db_session.add(sig)
    db_session.commit()
    db_session.refresh(sig)
    return sig


def _make_execution(
    db_session, signal_id: int, user_id: int, *, order_id: str = "POS-1", lot: float = 0.10
) -> Execution:
    ex = Execution(
        signal_id=signal_id,
        user_id=user_id,
        status=ExecutionStatus.filled,
        tradelocker_order_id=order_id,
        executed_lot_size=lot,
        fill_price=1.1000,
    )
    db_session.add(ex)
    db_session.commit()
    db_session.refresh(ex)
    return ex


# ---------------- _extract_forecast helper ----------------

def test_extract_forecast_pulls_field_from_json_payload(db_session, seed_bots):
    signal = _make_signal(db_session, seed_bots[0].id)
    assert _extract_forecast(signal, "forecast_drift") == 1.5
    assert _extract_forecast(signal, "forecast_confidence") == 2.0
    assert _extract_forecast(signal, "threshold") == 0.5


def test_extract_forecast_returns_none_for_missing_key(db_session, seed_bots):
    signal = _make_signal(db_session, seed_bots[0].id, raw_payload='{"other": 99}')
    assert _extract_forecast(signal, "forecast_drift") is None


def test_extract_forecast_returns_none_when_payload_empty(db_session, seed_bots):
    signal = _make_signal(db_session, seed_bots[0].id, raw_payload="")
    assert _extract_forecast(signal, "forecast_drift") is None


def test_extract_forecast_returns_none_when_payload_malformed(db_session, seed_bots):
    signal = _make_signal(db_session, seed_bots[0].id, raw_payload="not-json{{")
    assert _extract_forecast(signal, "forecast_drift") is None


# ---------------- track() ----------------

def test_track_registers_execution_with_zero_mae_mfe(db_session, seed_bots):
    user = _make_user(db_session)
    signal = _make_signal(db_session, seed_bots[0].id)
    ex = _make_execution(db_session, signal.id, user.id)

    pm = PositionMonitor(
        db_session_factory=_session_factory(db_session),
        client=MagicMock(),
        bot_id=seed_bots[0].id,
        timeframe="1m",
    )
    opened = datetime.utcnow() - timedelta(minutes=10)
    pm.track(ex, entry_price=1.1000, side="buy", opened_at=opened)
    state = pm._tracking[ex.id]
    assert state["mae"] == 0.0
    assert state["mfe"] == 0.0
    assert state["entry"] == 1.1000
    assert state["side"] == "buy"
    assert state["opened_at"] == opened


def test_track_defaults_opened_at_when_omitted(db_session, seed_bots):
    user = _make_user(db_session)
    signal = _make_signal(db_session, seed_bots[0].id)
    ex = _make_execution(db_session, signal.id, user.id)

    pm = PositionMonitor(
        db_session_factory=_session_factory(db_session),
        client=MagicMock(),
        bot_id=seed_bots[0].id,
        timeframe="1m",
    )
    before = datetime.utcnow()
    pm.track(ex, entry_price=1.1, side="sell")
    after = datetime.utcnow()
    assert before <= pm._tracking[ex.id]["opened_at"] <= after


# ---------------- poll_and_close — early exits ----------------

@pytest.mark.asyncio
async def test_poll_and_close_empty_when_nothing_tracked(db_session, seed_bots):
    pm = PositionMonitor(
        db_session_factory=_session_factory(db_session),
        client=MagicMock(),
        bot_id=seed_bots[0].id,
        timeframe="1m",
    )
    assert await pm.poll_and_close() == []


@pytest.mark.asyncio
async def test_poll_and_close_skips_user_without_token(db_session, seed_bots):
    """Tracked execution belongs to a user with no tradelocker creds — nothing
    happens (no broker call, no outcome)."""
    user = _make_user(db_session, connected=False)
    signal = _make_signal(db_session, seed_bots[0].id)
    ex = _make_execution(db_session, signal.id, user.id)

    client = AsyncMock()
    pm = PositionMonitor(
        db_session_factory=_session_factory(db_session),
        client=client,
        bot_id=seed_bots[0].id,
        timeframe="1m",
    )
    pm.track(ex, entry_price=1.1, side="buy")
    outcomes = await pm.poll_and_close()
    assert outcomes == []
    # Critically: the broker is NEVER queried when creds are missing
    client.get_positions.assert_not_called()


@pytest.mark.asyncio
async def test_poll_and_close_swallows_broker_error(db_session, seed_bots):
    """get_positions raising TradeLockerError shouldn't propagate — we just
    skip that user this round and try again next poll."""
    user = _make_user(db_session)
    signal = _make_signal(db_session, seed_bots[0].id)
    ex = _make_execution(db_session, signal.id, user.id)

    client = AsyncMock()
    client.get_positions.side_effect = TradeLockerError("broker down")
    pm = PositionMonitor(
        db_session_factory=_session_factory(db_session),
        client=client,
        bot_id=seed_bots[0].id,
        timeframe="1m",
    )
    pm.track(ex, entry_price=1.1, side="buy")
    outcomes = await pm.poll_and_close()
    assert outcomes == []
    # Tracking persists so the next poll can try again
    assert ex.id in pm._tracking


# ---------------- poll_and_close — MAE/MFE updates ----------------

@pytest.mark.asyncio
async def test_poll_and_close_updates_mfe_on_long_in_profit(db_session, seed_bots):
    """Long trade @1.10, current avg 1.11 → mfe = +0.01, mae stays 0."""
    user = _make_user(db_session)
    signal = _make_signal(db_session, seed_bots[0].id)
    ex = _make_execution(db_session, signal.id, user.id, order_id="POS-AAA")

    client = AsyncMock()
    client.get_positions.return_value = [
        {"id": "POS-AAA", "qty": 0.1, "avgPrice": 1.1100}
    ]
    pm = PositionMonitor(
        db_session_factory=_session_factory(db_session),
        client=client,
        bot_id=seed_bots[0].id,
        timeframe="1m",
    )
    pm.track(ex, entry_price=1.1000, side="buy")
    outcomes = await pm.poll_and_close()
    assert outcomes == []
    state = pm._tracking[ex.id]
    assert state["mfe"] == pytest.approx(0.01, abs=1e-9)
    assert state["mae"] == 0.0  # never went into drawdown


@pytest.mark.asyncio
async def test_poll_and_close_updates_mae_on_long_in_drawdown(db_session, seed_bots):
    """Long trade @1.10, current 1.09 → mae = -0.01, mfe stays 0."""
    user = _make_user(db_session)
    signal = _make_signal(db_session, seed_bots[0].id)
    ex = _make_execution(db_session, signal.id, user.id, order_id="POS-DD")

    client = AsyncMock()
    client.get_positions.return_value = [
        {"id": "POS-DD", "qty": 0.1, "avgPrice": 1.0900}
    ]
    pm = PositionMonitor(
        db_session_factory=_session_factory(db_session),
        client=client,
        bot_id=seed_bots[0].id,
        timeframe="1m",
    )
    pm.track(ex, entry_price=1.1000, side="buy")
    await pm.poll_and_close()
    state = pm._tracking[ex.id]
    assert state["mae"] == pytest.approx(-0.01, abs=1e-9)


@pytest.mark.asyncio
async def test_poll_and_close_short_side_inverts_delta(db_session, seed_bots):
    """Short trade @1.10, price drops to 1.09 → it's profit (delta = +0.01)."""
    user = _make_user(db_session)
    signal = _make_signal(db_session, seed_bots[0].id, side="sell")
    ex = _make_execution(db_session, signal.id, user.id, order_id="POS-SHORT")

    client = AsyncMock()
    client.get_positions.return_value = [
        {"id": "POS-SHORT", "qty": -0.1, "avgPrice": 1.0900}
    ]
    pm = PositionMonitor(
        db_session_factory=_session_factory(db_session),
        client=client,
        bot_id=seed_bots[0].id,
        timeframe="1m",
    )
    pm.track(ex, entry_price=1.1000, side="sell")
    await pm.poll_and_close()
    state = pm._tracking[ex.id]
    assert state["mfe"] == pytest.approx(0.01, abs=1e-9)


@pytest.mark.asyncio
async def test_poll_and_close_handles_malformed_avg_price(db_session, seed_bots):
    """Broker returns avgPrice=None (rare). We must NOT crash — treat as entry."""
    user = _make_user(db_session)
    signal = _make_signal(db_session, seed_bots[0].id)
    ex = _make_execution(db_session, signal.id, user.id, order_id="POS-NAN")

    client = AsyncMock()
    client.get_positions.return_value = [
        {"id": "POS-NAN", "qty": 0.1, "avgPrice": None}
    ]
    pm = PositionMonitor(
        db_session_factory=_session_factory(db_session),
        client=client,
        bot_id=seed_bots[0].id,
        timeframe="1m",
    )
    pm.track(ex, entry_price=1.1, side="buy")
    outcomes = await pm.poll_and_close()
    # Position still considered open; MAE/MFE unchanged from 0
    assert outcomes == []
    assert pm._tracking[ex.id]["mae"] == 0.0
    assert pm._tracking[ex.id]["mfe"] == 0.0


# ---------------- poll_and_close — close detection / TradeOutcome ----------------

@pytest.mark.asyncio
async def test_poll_and_close_creates_outcome_on_close_in_profit_fx(db_session, seed_bots):
    """Long EURUSD goes from 1.1000 to TP 1.1100 (then disappears from broker)
    → TradeOutcome built using TP as exit, FX 100k multiplier applied."""
    user = _make_user(db_session)
    signal = _make_signal(db_session, seed_bots[0].id, take_profit=1.1100)
    ex = _make_execution(db_session, signal.id, user.id, order_id="POS-TP", lot=0.1)

    client = AsyncMock()
    # Walk through the favorable move once, then position disappears
    states = [
        [{"id": "POS-TP", "qty": 0.1, "avgPrice": 1.1099}],  # in profit
        [],  # closed
    ]
    client.get_positions.side_effect = states

    pm = PositionMonitor(
        db_session_factory=_session_factory(db_session),
        client=client,
        bot_id=seed_bots[0].id,
        timeframe="1m",
    )
    opened = datetime.utcnow() - timedelta(minutes=30)
    pm.track(ex, entry_price=1.1000, side="buy", opened_at=opened)

    # First poll: still open, builds MFE
    await pm.poll_and_close()
    # Second poll: gone → outcome
    outcomes = await pm.poll_and_close()
    assert len(outcomes) == 1
    o = outcomes[0]
    assert o.instrument == "EURUSD"
    assert o.side == "buy"
    assert o.entry_price == 1.1000
    assert o.exit_price == 1.1100  # TP (because mfe >= |mae|)
    assert o.qty == 0.1
    # pnl = (1.11 - 1.10) * 0.1 * 100_000 = 100.0
    assert o.pnl_usd == pytest.approx(100.0, abs=1e-6)
    # R-multiple = 0.01 / |1.10-1.095| = 0.01/0.005 = 2.0
    assert o.r_multiple == pytest.approx(2.0, abs=1e-6)
    assert o.hold_seconds >= 0
    # Tracking cleaned up
    assert ex.id not in pm._tracking


@pytest.mark.asyncio
async def test_poll_and_close_crypto_uses_unit_multiplier(db_session, seed_bots):
    """BTCUSD: 1 lot = 1 BTC — multiplier 1.0, NOT 100_000. Regression for
    the 2026-05-10 P&L explosion bug."""
    user = _make_user(db_session)
    bot = next(b for b in seed_bots if b.strategy_type == StrategyType.latpfn_quant)
    signal = _make_signal(
        db_session,
        bot.id,
        instrument="BTCUSD",
        entry_price=80_000.0,
        stop_loss=79_900.0,
        take_profit=80_100.0,
    )
    ex = _make_execution(db_session, signal.id, user.id, order_id="BTC-1", lot=0.01)

    client = AsyncMock()
    client.get_positions.return_value = []  # already gone — closed
    pm = PositionMonitor(
        db_session_factory=_session_factory(db_session),
        client=client,
        bot_id=bot.id,
        timeframe="1m",
    )
    # No MFE/MAE moves recorded → mae(0) == mfe(0) → mfe >= |mae| → exits at TP
    pm.track(ex, entry_price=80_000.0, side="buy")
    outcomes = await pm.poll_and_close()
    assert len(outcomes) == 1
    o = outcomes[0]
    # PnL = (80_100 - 80_000) * 0.01 * 1.0 = 1.00 (NOT 100_000)
    assert o.pnl_usd == pytest.approx(1.0, abs=1e-6)


@pytest.mark.asyncio
async def test_poll_and_close_metals_uses_100x_multiplier(db_session, seed_bots):
    """XAUUSD: 1 lot = 100 oz. multiplier 100."""
    user = _make_user(db_session)
    bot = seed_bots[0]
    signal = _make_signal(
        db_session,
        bot.id,
        instrument="XAUUSD",
        entry_price=2_000.0,
        stop_loss=1_995.0,
        take_profit=2_010.0,
    )
    ex = _make_execution(db_session, signal.id, user.id, order_id="GOLD-1", lot=0.1)

    client = AsyncMock()
    client.get_positions.return_value = []  # closed
    pm = PositionMonitor(
        db_session_factory=_session_factory(db_session),
        client=client,
        bot_id=bot.id,
        timeframe="1m",
    )
    pm.track(ex, entry_price=2_000.0, side="buy")
    outcomes = await pm.poll_and_close()
    assert len(outcomes) == 1
    # PnL = (2010-2000) * 0.1 * 100 = 100
    assert outcomes[0].pnl_usd == pytest.approx(100.0, abs=1e-6)


@pytest.mark.asyncio
async def test_poll_and_close_jpy_uses_1000x_multiplier(db_session, seed_bots):
    """USDJPY: 1 lot = 1000 (special). multiplier 1000."""
    user = _make_user(db_session)
    bot = seed_bots[0]
    signal = _make_signal(
        db_session,
        bot.id,
        instrument="USDJPY",
        entry_price=150.00,
        stop_loss=149.50,
        take_profit=151.00,
    )
    ex = _make_execution(db_session, signal.id, user.id, order_id="JPY-1", lot=0.1)

    client = AsyncMock()
    client.get_positions.return_value = []
    pm = PositionMonitor(
        db_session_factory=_session_factory(db_session),
        client=client,
        bot_id=bot.id,
        timeframe="1m",
    )
    pm.track(ex, entry_price=150.00, side="buy")
    outcomes = await pm.poll_and_close()
    # PnL = (151 - 150) * 0.1 * 1000 = 100
    assert outcomes[0].pnl_usd == pytest.approx(100.0, abs=1e-6)


@pytest.mark.asyncio
async def test_poll_and_close_creates_outcome_on_close_at_sl_when_mae_worse(db_session, seed_bots):
    """If we ever went into drawdown deeper than profit (|mae| > mfe),
    the heuristic assumes we got stopped out → exit_price = SL.
    """
    user = _make_user(db_session)
    signal = _make_signal(db_session, seed_bots[0].id, stop_loss=1.0950)
    ex = _make_execution(db_session, signal.id, user.id, order_id="SL-X")

    client = AsyncMock()
    states = [
        [{"id": "SL-X", "qty": 0.1, "avgPrice": 1.0945}],  # deep drawdown
        [],  # closed
    ]
    client.get_positions.side_effect = states

    pm = PositionMonitor(
        db_session_factory=_session_factory(db_session),
        client=client,
        bot_id=seed_bots[0].id,
        timeframe="1m",
    )
    pm.track(ex, entry_price=1.1000, side="buy")
    await pm.poll_and_close()  # records MAE
    outcomes = await pm.poll_and_close()
    assert outcomes[0].exit_price == 1.0950
    # PnL negative (FX): (1.095 - 1.10) * 0.1 * 100k = -50
    assert outcomes[0].pnl_usd == pytest.approx(-50.0, abs=1e-6)


@pytest.mark.asyncio
async def test_poll_and_close_outcome_carries_forecast_context(db_session, seed_bots):
    """Forecast drift/confidence/threshold copied from signal.raw_payload."""
    user = _make_user(db_session)
    signal = _make_signal(db_session, seed_bots[0].id)
    ex = _make_execution(db_session, signal.id, user.id)

    client = AsyncMock()
    client.get_positions.return_value = []  # closed immediately
    pm = PositionMonitor(
        db_session_factory=_session_factory(db_session),
        client=client,
        bot_id=seed_bots[0].id,
        timeframe="1m",
    )
    pm.track(ex, entry_price=1.1, side="buy")
    outcomes = await pm.poll_and_close()
    assert outcomes[0].forecast_drift == 1.5
    assert outcomes[0].forecast_confidence == 2.0
    assert outcomes[0].threshold_at_entry == 0.5


@pytest.mark.asyncio
async def test_poll_and_close_zero_sl_distance_returns_zero_r(db_session, seed_bots):
    """If SL distance is ~0 we divide-by-zero risk. Code must return r=0 not crash."""
    user = _make_user(db_session)
    # entry == stop → risked == 0 → r_multiple must default to 0
    signal = _make_signal(
        db_session, seed_bots[0].id, entry_price=1.10, stop_loss=1.10, take_profit=1.11
    )
    ex = _make_execution(db_session, signal.id, user.id)

    client = AsyncMock()
    client.get_positions.return_value = []
    pm = PositionMonitor(
        db_session_factory=_session_factory(db_session),
        client=client,
        bot_id=seed_bots[0].id,
        timeframe="1m",
    )
    pm.track(ex, entry_price=1.10, side="buy")
    outcomes = await pm.poll_and_close()
    assert outcomes[0].r_multiple == 0.0


@pytest.mark.asyncio
async def test_poll_and_close_persists_outcome_to_db(db_session, seed_bots):
    """After poll_and_close, TradeOutcome row must exist in the DB."""
    user = _make_user(db_session)
    signal = _make_signal(db_session, seed_bots[0].id)
    ex = _make_execution(db_session, signal.id, user.id)

    client = AsyncMock()
    client.get_positions.return_value = []  # closed immediately
    pm = PositionMonitor(
        db_session_factory=_session_factory(db_session),
        client=client,
        bot_id=seed_bots[0].id,
        timeframe="1m",
    )
    pm.track(ex, entry_price=1.10, side="buy")
    await pm.poll_and_close()

    persisted = db_session.query(TradeOutcome).filter_by(signal_id=signal.id).all()
    assert len(persisted) == 1
    assert persisted[0].bot_id == seed_bots[0].id
    assert persisted[0].timeframe == "1m"


@pytest.mark.asyncio
async def test_poll_and_close_returns_none_when_signal_missing(db_session, seed_bots):
    """If _close_outcome can't find the parent Signal row (race / cleanup),
    it must return None — and the caller skips adding to outcomes list.
    This directly exercises the `signal is None: return None` branch.
    """
    from app.strategies.position_monitor import PositionMonitor as _PM

    user = _make_user(db_session)
    signal = _make_signal(db_session, seed_bots[0].id)
    ex = _make_execution(db_session, signal.id, user.id)

    pm = _PM(
        db_session_factory=_session_factory(db_session),
        client=AsyncMock(),
        bot_id=seed_bots[0].id,
        timeframe="1m",
    )
    pm.track(ex, entry_price=1.1, side="buy")

    # Pass a fake-id execution that won't resolve to a signal
    fake_ex = Execution(
        signal_id=999_999,  # non-existent
        user_id=user.id,
        status=ExecutionStatus.filled,
    )
    track_data = {"mae": 0.0, "mfe": 0.0, "entry": 1.1, "side": "buy",
                  "opened_at": datetime.utcnow()}
    result = await pm._close_outcome(db_session, fake_ex, track_data)
    assert result is None
