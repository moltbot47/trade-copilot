"""Unit tests for the signal fan-out router (app/core/signal_router.py).

The fan_out function is the bridge between an inbound signal and broker
orders for every subscriber of that bot. Before these tests it was 21%
covered — every call site mocked it out. Cover the high-value branches:

  - Skip inactive users
  - Skip users without a TradeLocker connection (record a rejected exec)
  - Skip paused subscriptions
  - Fall back to default balance when the broker rejects the state call
  - Mark execution as `error` (not `filled`) when place_order raises
  - Mark as `filled` and carry the order_id on success
  - Respect each subscriber's per-account aggression
"""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from app.core.crypto import encrypt
from app.core.signal_router import fan_out
from app.core.tradelocker_client import TradeLockerError
from app.db.models import (
    Execution,
    ExecutionStatus,
    Signal,
    Subscription,
    User,
)


def _make_user(db_session, email: str, *, connected: bool = True, active: bool = True) -> User:
    user = User(email=email, hashed_password="x", is_active=active)
    if connected:
        user.tradelocker_account_id = "ACC-1"
        user.tradelocker_acc_num = "1"
        user.tradelocker_env = "demo"
        user.tradelocker_token = encrypt("fake-access-token")
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
        raw_payload="{}",
    )
    defaults.update(overrides)
    sig = Signal(**defaults)
    db_session.add(sig)
    db_session.commit()
    db_session.refresh(sig)
    return sig


def _subscribe(db_session, user_id: int, bot_id: int, aggression: int = 5, paused: bool = False):
    sub = Subscription(
        user_id=user_id, bot_id=bot_id, aggression_level=aggression, is_paused=paused
    )
    db_session.add(sub)
    db_session.commit()


@pytest.mark.asyncio
async def test_fan_out_no_subscribers_returns_empty(db_session, seed_bots):
    bot = seed_bots[0]
    signal = _make_signal(db_session, bot.id)
    # Nobody subscribed
    result = await fan_out(signal, db_session)
    assert result == []


@pytest.mark.asyncio
async def test_fan_out_skips_paused_subscription(db_session, seed_bots):
    """is_paused=True means the user opted out — no execution row at all."""
    bot = seed_bots[0]
    user = _make_user(db_session, "paused@example.com")
    _subscribe(db_session, user.id, bot.id, paused=True)
    signal = _make_signal(db_session, bot.id)

    result = await fan_out(signal, db_session)
    assert result == []
    # And no executions persisted
    assert db_session.query(Execution).count() == 0


@pytest.mark.asyncio
async def test_fan_out_skips_inactive_user(db_session, seed_bots):
    """An inactive user (is_active=False) is silently skipped, no exec row."""
    bot = seed_bots[0]
    user = _make_user(db_session, "inactive@example.com", active=False)
    _subscribe(db_session, user.id, bot.id)
    signal = _make_signal(db_session, bot.id)

    result = await fan_out(signal, db_session)
    assert result == []
    assert db_session.query(Execution).count() == 0


@pytest.mark.asyncio
async def test_fan_out_no_tradelocker_creates_rejected_execution(db_session, seed_bots):
    """A subscriber without a TL token gets a 'rejected' execution row
    explaining why — this is the audit trail showing why they didn't trade.
    """
    bot = seed_bots[0]
    user = _make_user(db_session, "no-broker@example.com", connected=False)
    _subscribe(db_session, user.id, bot.id)
    signal = _make_signal(db_session, bot.id)

    result = await fan_out(signal, db_session)
    assert len(result) == 1
    ex = result[0]
    assert ex.status == ExecutionStatus.rejected
    assert ex.user_id == user.id
    assert ex.signal_id == signal.id
    assert "no connected" in (ex.error_message or "").lower()


@pytest.mark.asyncio
async def test_fan_out_records_filled_execution_with_order_id(db_session, seed_bots):
    """Happy path: state + place_order succeed; execution status=filled with the broker order id."""
    bot = seed_bots[0]
    user = _make_user(db_session, "happy@example.com")
    _subscribe(db_session, user.id, bot.id, aggression=5)
    signal = _make_signal(db_session, bot.id)

    fake_state = AsyncMock(return_value={"balance": 25_000.0})
    fake_order = AsyncMock(return_value={"orderId": "BROKER-ORD-555"})

    with patch(
        "app.core.signal_router.TradeLockerClient.get_account_state", new=fake_state
    ), patch(
        "app.core.signal_router.TradeLockerClient.place_order", new=fake_order
    ):
        result = await fan_out(signal, db_session)

    assert len(result) == 1
    ex = result[0]
    assert ex.status == ExecutionStatus.filled
    assert ex.tradelocker_order_id == "BROKER-ORD-555"
    assert ex.fill_price == signal.entry_price
    assert ex.executed_lot_size is not None and ex.executed_lot_size > 0


@pytest.mark.asyncio
async def test_fan_out_falls_back_to_default_balance_on_state_error(db_session, seed_bots):
    """If get_account_state raises TradeLockerError, we should still attempt
    the order using a 10k default balance — not crash the whole fan-out."""
    bot = seed_bots[0]
    user = _make_user(db_session, "noState@example.com")
    _subscribe(db_session, user.id, bot.id)
    signal = _make_signal(db_session, bot.id)

    fake_state = AsyncMock(side_effect=TradeLockerError("state lookup down"))
    fake_order = AsyncMock(return_value={"order_id": "ORD-X"})

    with patch(
        "app.core.signal_router.TradeLockerClient.get_account_state", new=fake_state
    ), patch(
        "app.core.signal_router.TradeLockerClient.place_order", new=fake_order
    ):
        result = await fan_out(signal, db_session)

    assert len(result) == 1
    ex = result[0]
    # Order still placed and filled despite the state lookup failure
    assert ex.status == ExecutionStatus.filled
    assert ex.tradelocker_order_id == "ORD-X"


@pytest.mark.asyncio
async def test_fan_out_marks_execution_error_on_place_order_failure(db_session, seed_bots):
    """When place_order raises TradeLockerError, execution must be 'error'
    with the broker message captured (truncated to 500 chars).
    """
    bot = seed_bots[0]
    user = _make_user(db_session, "broken@example.com")
    _subscribe(db_session, user.id, bot.id)
    signal = _make_signal(db_session, bot.id)

    fake_state = AsyncMock(return_value={"balance": 10_000.0})
    fake_order = AsyncMock(side_effect=TradeLockerError("insufficient margin"))

    with patch(
        "app.core.signal_router.TradeLockerClient.get_account_state", new=fake_state
    ), patch(
        "app.core.signal_router.TradeLockerClient.place_order", new=fake_order
    ):
        result = await fan_out(signal, db_session)

    assert len(result) == 1
    ex = result[0]
    assert ex.status == ExecutionStatus.error
    assert ex.error_message is not None
    assert "insufficient margin" in ex.error_message


@pytest.mark.asyncio
async def test_fan_out_error_message_truncated_to_500_chars(db_session, seed_bots):
    """Broker can return arbitrarily-long error strings; we cap to 500
    chars before persisting to keep the column bounded."""
    bot = seed_bots[0]
    user = _make_user(db_session, "longerr@example.com")
    _subscribe(db_session, user.id, bot.id)
    signal = _make_signal(db_session, bot.id)

    huge_msg = "X" * 2000
    fake_state = AsyncMock(return_value={"balance": 10_000.0})
    fake_order = AsyncMock(side_effect=TradeLockerError(huge_msg))

    with patch(
        "app.core.signal_router.TradeLockerClient.get_account_state", new=fake_state
    ), patch(
        "app.core.signal_router.TradeLockerClient.place_order", new=fake_order
    ):
        result = await fan_out(signal, db_session)

    assert result[0].error_message is not None
    assert len(result[0].error_message) <= 500


@pytest.mark.asyncio
async def test_fan_out_aggression_affects_lot_size(db_session, seed_bots):
    """Two subs on the same signal with different aggression must get
    different executed lot sizes (more aggressive → larger lot)."""
    bot = seed_bots[0]
    timid = _make_user(db_session, "timid@example.com")
    bold = _make_user(db_session, "bold@example.com")
    _subscribe(db_session, timid.id, bot.id, aggression=1)
    _subscribe(db_session, bold.id, bot.id, aggression=10)
    signal = _make_signal(db_session, bot.id, base_lot_size=0.10)

    fake_state = AsyncMock(return_value={"balance": 50_000.0})
    fake_order = AsyncMock(return_value={"id": "ORD-1"})

    with patch(
        "app.core.signal_router.TradeLockerClient.get_account_state", new=fake_state
    ), patch(
        "app.core.signal_router.TradeLockerClient.place_order", new=fake_order
    ):
        result = await fan_out(signal, db_session)

    by_user = {ex.user_id: ex for ex in result}
    timid_lot = by_user[timid.id].executed_lot_size or 0
    bold_lot = by_user[bold.id].executed_lot_size or 0
    assert bold_lot > timid_lot, (
        f"aggression=10 lot ({bold_lot}) should exceed aggression=1 lot ({timid_lot})"
    )


@pytest.mark.asyncio
async def test_fan_out_handles_balance_in_nested_account_state(db_session, seed_bots):
    """TradeLocker sometimes returns the balance inside accountState dict.
    The router must dig into that shape, not fall back to the default 10k.
    """
    bot = seed_bots[0]
    user = _make_user(db_session, "nested@example.com")
    _subscribe(db_session, user.id, bot.id)
    signal = _make_signal(db_session, bot.id)

    fake_state = AsyncMock(
        return_value={"accountState": {"balance": 75_000.0}}
    )
    captured_kwargs = {}

    async def _capture_place_order(**kwargs):
        captured_kwargs.update(kwargs)
        return {"orderId": "OK"}

    with patch(
        "app.core.signal_router.TradeLockerClient.get_account_state", new=fake_state
    ), patch(
        "app.core.signal_router.TradeLockerClient.place_order",
        new=AsyncMock(side_effect=_capture_place_order),
    ):
        result = await fan_out(signal, db_session)

    # Order placed with expected symbol/side from the signal
    assert captured_kwargs["symbol"] == "EURUSD"
    assert captured_kwargs["side"] == "buy"
    assert captured_kwargs["sl"] == signal.stop_loss
    assert captured_kwargs["tp"] == signal.take_profit
    assert result[0].status == ExecutionStatus.filled


@pytest.mark.asyncio
async def test_fan_out_persists_executions_atomically(db_session, seed_bots):
    """After fan_out returns, all execution rows must be visible in the DB
    (commit happened). The caller relies on this for the trade outbox.
    """
    bot = seed_bots[0]
    u1 = _make_user(db_session, "p1@example.com")
    u2 = _make_user(db_session, "p2@example.com", connected=False)  # rejected path
    _subscribe(db_session, u1.id, bot.id)
    _subscribe(db_session, u2.id, bot.id)
    signal = _make_signal(db_session, bot.id)

    fake_state = AsyncMock(return_value={"balance": 10_000.0})
    fake_order = AsyncMock(return_value={"orderId": "ABC"})

    with patch(
        "app.core.signal_router.TradeLockerClient.get_account_state", new=fake_state
    ), patch(
        "app.core.signal_router.TradeLockerClient.place_order", new=fake_order
    ):
        result = await fan_out(signal, db_session)

    persisted = db_session.query(Execution).filter_by(signal_id=signal.id).all()
    assert len(persisted) == 2 == len(result)
    statuses = {ex.status for ex in persisted}
    assert ExecutionStatus.filled in statuses
    assert ExecutionStatus.rejected in statuses


# -----------------------------------------------------------------------
# Per-user instrument filter (Subscription.allowed_instruments)
# -----------------------------------------------------------------------

def _subscribe_with_filter(
    db_session, user_id: int, bot_id: int, allowed_csv: str | None, aggression: int = 5
):
    """Variant of _subscribe that sets allowed_instruments on the row."""
    sub = Subscription(
        user_id=user_id,
        bot_id=bot_id,
        aggression_level=aggression,
        is_paused=False,
        allowed_instruments=allowed_csv,
    )
    db_session.add(sub)
    db_session.commit()


@pytest.mark.asyncio
async def test_fan_out_skips_subscriber_outside_allowed_instruments(db_session, seed_bots):
    """When allowed_instruments is set and the signal's instrument isn't in
    the user's filter, the subscriber is silently skipped — no execution row.
    A second subscriber with NULL filter (= all instruments) still gets the
    fill, proving the filter is opt-in and doesn't break legacy rows."""
    bot = seed_bots[0]
    eur_only = _make_user(db_session, "eur-only@example.com")
    all_user = _make_user(db_session, "all@example.com")
    # eur-only opts in to EURUSD only; all_user has no filter (legacy / default).
    _subscribe_with_filter(db_session, eur_only.id, bot.id, "EURUSD")
    _subscribe_with_filter(db_session, all_user.id, bot.id, None)

    # Fire a GBPUSD signal — eur-only must be skipped, all_user must be filled.
    signal = _make_signal(db_session, bot.id, instrument="GBPUSD")
    fake_state = AsyncMock(return_value={"balance": 10_000.0})
    fake_order = AsyncMock(return_value={"orderId": "GBP-1"})

    with patch(
        "app.core.signal_router.TradeLockerClient.get_account_state", new=fake_state
    ), patch(
        "app.core.signal_router.TradeLockerClient.place_order", new=fake_order
    ):
        result = await fan_out(signal, db_session)

    # Only one execution — the unfiltered subscriber.
    assert len(result) == 1
    assert result[0].user_id == all_user.id
    assert result[0].status == ExecutionStatus.filled
    # No execution row was created for the filtered-out user.
    eur_only_rows = (
        db_session.query(Execution)
        .filter(Execution.user_id == eur_only.id)
        .all()
    )
    assert eur_only_rows == []


@pytest.mark.asyncio
async def test_fan_out_includes_subscriber_inside_allowed_instruments(db_session, seed_bots):
    """When the signal's instrument IS in the filter, the subscriber gets a
    normal fill — the filter only narrows, never amplifies."""
    bot = seed_bots[0]
    user = _make_user(db_session, "eur-only-pass@example.com")
    _subscribe_with_filter(db_session, user.id, bot.id, "EURUSD,GBPUSD")

    signal = _make_signal(db_session, bot.id, instrument="eurusd")  # case-insensitive
    fake_state = AsyncMock(return_value={"balance": 10_000.0})
    fake_order = AsyncMock(return_value={"orderId": "EUR-1"})

    with patch(
        "app.core.signal_router.TradeLockerClient.get_account_state", new=fake_state
    ), patch(
        "app.core.signal_router.TradeLockerClient.place_order", new=fake_order
    ):
        result = await fan_out(signal, db_session)

    assert len(result) == 1
    assert result[0].user_id == user.id
    assert result[0].status == ExecutionStatus.filled
