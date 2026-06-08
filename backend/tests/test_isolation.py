"""Tests for the strategy-isolation harness.

Covers: StrategyAccount model + isolation invariants, PositionMonitor
per-account attribution (the clean-equity guarantee), IsolatedRunner
validation + registry, and the /api/isolation endpoints.
"""
from __future__ import annotations

from datetime import datetime
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from app.core.crypto import encrypt
from app.db.models import (
    Execution,
    ExecutionStatus,
    Signal,
    StrategyAccount,
    StrategyType,
    TradeOutcome,
    User,
)
from app.strategies.isolation import IsolatedRunner, get_iso_runner
from app.strategies.position_monitor import PositionMonitor


def _factory(db_engine):
    """A real session factory bound to the per-test engine (StaticPool so
    every session sees the same in-memory schema/state)."""
    return sessionmaker(
        autocommit=False, autoflush=False, bind=db_engine, future=True
    )


def _mk_account(db_session, bot, user, **kw):
    sa = StrategyAccount(
        bot_id=bot.id,
        user_id=user.id,
        label=kw.get("label", "iso"),
        tradelocker_account_id=kw.get("acct", "9001"),
        tradelocker_acc_num=kw.get("acc_num", "4"),
        tradelocker_env="demo",
        timeframe=kw.get("timeframe", "1m"),
        start_offset_seconds=kw.get("offset", 0),
    )
    db_session.add(sa)
    db_session.commit()
    db_session.refresh(sa)
    return sa


# --------------------------------------------------------------------- #
# model + isolation invariants
# --------------------------------------------------------------------- #
def test_strategy_account_one_per_bot(db_session, seed_bots):
    user = User(email="o@x.com", hashed_password="x")
    db_session.add(user)
    db_session.commit()
    bot = seed_bots[3]  # latpfn-momentum
    _mk_account(db_session, bot, user, acct="100")

    dup = StrategyAccount(
        bot_id=bot.id,
        user_id=user.id,
        tradelocker_account_id="101",
    )
    db_session.add(dup)
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()


def test_strategy_account_one_strategy_per_demo_account(db_session, seed_bots):
    user = User(email="o@x.com", hashed_password="x")
    db_session.add(user)
    db_session.commit()
    _mk_account(db_session, seed_bots[3], user, acct="555")

    # Different bot, SAME demo account → blocked. This is the core
    # isolation guarantee: two strategies can never share an account.
    dup = StrategyAccount(
        bot_id=seed_bots[4].id,
        user_id=user.id,
        tradelocker_account_id="555",
    )
    db_session.add(dup)
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()


# --------------------------------------------------------------------- #
# PositionMonitor — clean per-account attribution
# --------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_position_monitor_stamps_strategy_account_id(
    db_session, db_engine, seed_bots
):
    """A trade closed under the isolation harness must carry its
    strategy_account_id so the isolation equity curve filters cleanly."""
    user = User(
        email="m@x.com",
        hashed_password="x",
        tradelocker_token=encrypt("tok"),
        tradelocker_account_id="DEFAULT-ACCT",
        tradelocker_acc_num="1",
    )
    db_session.add(user)
    db_session.commit()
    bot = seed_bots[3]

    sig = Signal(
        bot_id=bot.id,
        instrument="BTCUSD",
        side="buy",
        entry_price=100.0,
        stop_loss=95.0,
        take_profit=110.0,
        base_lot_size=0.01,
        raw_payload="{}",
    )
    db_session.add(sig)
    db_session.commit()
    ex = Execution(
        signal_id=sig.id,
        user_id=user.id,
        status=ExecutionStatus.filled,
        tradelocker_order_id="POS-1",
        executed_lot_size=0.01,
        fill_price=100.0,
    )
    db_session.add(ex)
    db_session.commit()

    client = AsyncMock()
    # Position vanished from the book → monitor treats it as closed.
    client.get_positions = AsyncMock(return_value=[])

    pm = PositionMonitor(
        _factory(db_engine),
        client,
        bot.id,
        "1m",
        account_override=("ISO-ACCT-9001", "4"),
        strategy_account_id=4242,
    )
    pm.track(ex, entry_price=100.0, side="buy", opened_at=datetime.utcnow())
    outcomes = await pm.poll_and_close()

    # It polled the ISOLATED sub-account, not the user's default account.
    client.get_positions.assert_awaited()
    assert client.get_positions.await_args.args[0] == "ISO-ACCT-9001"
    assert len(outcomes) == 1
    assert outcomes[0].strategy_account_id == 4242

    row = db_session.query(TradeOutcome).one()
    assert row.strategy_account_id == 4242
    assert row.bot_id == bot.id


def test_position_monitor_default_path_unchanged(db_engine, seed_bots):
    """Without an override the monitor keeps the legacy behavior (None
    strategy_account_id, resolves account from the user)."""
    pm = PositionMonitor(_factory(db_engine), AsyncMock(), 1, "1m")
    assert pm.strategy_account_id is None
    assert pm._account_override is None


# --------------------------------------------------------------------- #
# IsolatedRunner — validation + registry
# --------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_runner_rejects_webhook_strategy(db_session, db_engine, seed_bots):
    user = User(email="o@x.com", hashed_password="x")
    db_session.add(user)
    db_session.commit()
    orb_bot = seed_bots[0]  # webhook-driven, unsupported
    sa = _mk_account(db_session, orb_bot, user, acct="1")
    with pytest.raises(ValueError, match="isolation harness supports"):
        await IsolatedRunner.start(_factory(db_engine), sa.id)


@pytest.mark.asyncio
async def test_runner_rejects_bot_without_instruments(
    db_session, db_engine
):
    from app.db.models import Bot

    user = User(email="o@x.com", hashed_password="x")
    bot = Bot(
        name="Empty LaT",
        slug="empty-lat",
        strategy_type=StrategyType.latpfn_momentum,
        instruments_csv="",
        webhook_secret="s",
    )
    db_session.add_all([user, bot])
    db_session.commit()
    sa = _mk_account(db_session, bot, user, acct="2")
    with pytest.raises(ValueError, match="no instruments"):
        await IsolatedRunner.start(_factory(db_engine), sa.id)


@pytest.mark.asyncio
async def test_runner_starts_and_is_idempotent(db_session, db_engine, seed_bots):
    """A supported bot starts; the loop self-aborts (owning user has no
    broker token) but the registry binds one runner per bot and start()
    is idempotent."""
    user = User(email="o@x.com", hashed_password="x")  # no TL token
    db_session.add(user)
    db_session.commit()
    bot = seed_bots[4]  # latpfn-quant, instruments BTCUSD
    sa = _mk_account(db_session, bot, user, acct="3", timeframe="5m", offset=7)

    r1 = await IsolatedRunner.start(_factory(db_engine), sa.id)
    try:
        assert r1.bot_id == bot.id
        assert r1.timeframe == "5m"
        assert r1.symbols == ["BTCUSD"]
        assert get_iso_runner(bot.id) is r1
        r2 = await IsolatedRunner.start(_factory(db_engine), sa.id)
        assert r2 is r1  # idempotent — no second runner for the same bot
    finally:
        await r1.stop()
        assert get_iso_runner(bot.id) is None


# --------------------------------------------------------------------- #
# API
# --------------------------------------------------------------------- #
def _link_broker(db_session, email: str) -> None:
    u = db_session.query(User).filter(User.email == email).first()
    u.tradelocker_token = encrypt("faketoken")
    u.tradelocker_env = "demo"
    db_session.commit()


_FAKE_ACCOUNTS = [
    {"id": "9001", "accNum": "4", "name": "Demo A", "accountBalance": 50000, "currency": "USD"},
    {"id": "9002", "accNum": "5", "name": "Demo B", "accountBalance": 50000, "currency": "USD"},
]


def test_api_register_list_and_invariants(
    client, auth_headers, auth_email, db_session, seed_bots
):
    _link_broker(db_session, auth_email)
    momentum, quant = seed_bots[3], seed_bots[4]

    with patch(
        "app.api.isolation.TradeLockerClient.list_all_accounts",
        new=AsyncMock(return_value=_FAKE_ACCOUNTS),
    ):
        # available-accounts shows both, unbound
        r = client.get("/api/isolation/available-accounts", headers=auth_headers)
        assert r.status_code == 200
        assert {a["account_id"] for a in r.json()["accounts"]} == {"9001", "9002"}
        assert all(a["bound_to_bot_id"] is None for a in r.json()["accounts"])

        # register momentum → 9001
        r = client.post(
            "/api/isolation/accounts",
            headers=auth_headers,
            json={"bot_id": momentum.id, "tradelocker_account_id": "9001"},
        )
        assert r.status_code == 200, r.text
        assert r.json()["tradelocker_acc_num"] == "4"  # taken from broker

        # same bot again → 409
        r = client.post(
            "/api/isolation/accounts",
            headers=auth_headers,
            json={"bot_id": momentum.id, "tradelocker_account_id": "9002"},
        )
        assert r.status_code == 409

        # different bot, same account → 409 (isolation invariant)
        r = client.post(
            "/api/isolation/accounts",
            headers=auth_headers,
            json={"bot_id": quant.id, "tradelocker_account_id": "9001"},
        )
        assert r.status_code == 409

        # unknown account id → 400
        r = client.post(
            "/api/isolation/accounts",
            headers=auth_headers,
            json={"bot_id": quant.id, "tradelocker_account_id": "0000"},
        )
        assert r.status_code == 400

    r = client.get("/api/isolation/accounts", headers=auth_headers)
    assert r.status_code == 200
    accts = r.json()["accounts"]
    assert len(accts) == 1
    assert accts[0]["bot_id"] == momentum.id


def test_api_register_rejects_webhook_bot(
    client, auth_headers, auth_email, db_session, seed_bots
):
    _link_broker(db_session, auth_email)
    r = client.post(
        "/api/isolation/accounts",
        headers=auth_headers,
        json={"bot_id": seed_bots[0].id, "tradelocker_account_id": "9001"},
    )
    assert r.status_code == 400
    assert "isolation supports" in r.json()["detail"]


def test_api_start_requires_registration(client, auth_headers, seed_bots):
    r = client.post(
        "/api/isolation/start",
        headers=auth_headers,
        json={"bot_id": seed_bots[3].id},
    )
    assert r.status_code == 404


def test_api_stop_when_not_running(client, auth_headers, db_session, seed_bots):
    """Stop returns not_running ONLY when the caller already owns a binding
    for this bot but nothing's actively spawned. With no binding the API
    returns 404 so attackers can't enumerate which bots have isolated
    accounts (changed when auth scoping was added)."""
    from app.api.users import get_or_create_user
    auth_user = get_or_create_user(db_session, "tester@example.com")
    _mk_account(db_session, seed_bots[3], auth_user, acct="stop-test")

    r = client.post(
        "/api/isolation/stop",
        headers=auth_headers,
        json={"bot_id": seed_bots[3].id},
    )
    assert r.status_code == 200
    assert r.json()["status"] == "not_running"


def test_api_stop_returns_404_for_unbound_bot(client, auth_headers, seed_bots):
    """No StrategyAccount on this bot for the caller → 404. Prevents
    bot_id enumeration via the stop endpoint."""
    r = client.post(
        "/api/isolation/stop",
        headers=auth_headers,
        json={"bot_id": seed_bots[3].id},
    )
    assert r.status_code == 404


def test_api_stop_rejects_unauthenticated(client, seed_bots):
    """No session at all → 401, not silently succeeds."""
    r = client.post(
        "/api/isolation/stop",
        json={"bot_id": seed_bots[3].id},
    )
    assert r.status_code in (401, 403)


def test_api_start_rejects_unauthenticated(client, seed_bots):
    """The big one: anonymous requests cannot start isolated runners.
    This was the headline security gap before the auth wiring."""
    r = client.post(
        "/api/isolation/start",
        json={"bot_id": seed_bots[3].id},
    )
    assert r.status_code in (401, 403)


def test_api_equity_rejects_unauthenticated(client, seed_bots):
    r = client.get(f"/api/isolation/equity?bot_id={seed_bots[3].id}")
    assert r.status_code in (401, 403)


def test_api_compare_rejects_unauthenticated(client):
    r = client.get("/api/isolation/compare")
    assert r.status_code in (401, 403)


def test_api_compare_scopes_to_caller(
    client, auth_headers, db_session, seed_bots
):
    """User A's compare must not surface User B's strategy accounts.
    Prevents cross-user equity-curve leak."""
    from app.api.users import get_or_create_user
    auth_user = get_or_create_user(db_session, "tester@example.com")
    other = User(email="other@x.com", hashed_password="x")
    db_session.add(other)
    db_session.commit()
    _mk_account(db_session, seed_bots[3], auth_user, acct="auth-acct")
    _mk_account(db_session, seed_bots[4], other, acct="other-acct")

    r = client.get("/api/isolation/compare", headers=auth_headers)
    assert r.status_code == 200
    strategies = r.json()["strategies"]
    assert len(strategies) == 1
    assert strategies[0]["tradelocker_account_id"] == "auth-acct"


def test_api_equity_and_compare_filter_by_account(
    client, auth_headers, db_session, seed_bots
):
    """Equity/compare must only see this strategy's own trades — never
    another strategy's or a copy-runner's. Caller is auth'd as
    tester@example.com so the StrategyAccount must be bound to that
    user for the endpoint's owner-scoping to succeed."""
    from app.api.users import get_or_create_user
    user = get_or_create_user(db_session, "tester@example.com")
    bot = seed_bots[3]
    sa = _mk_account(db_session, bot, user, acct="7777")

    base = dict(
        bot_id=bot.id,
        instrument="BTCUSD",
        side="buy",
        timeframe="1m",
        entry_price=100.0,
        exit_price=110.0,
        qty=0.01,
        opened_at=datetime.utcnow(),
        closed_at=datetime.utcnow(),
    )
    # Two trades attributed to THIS isolated account...
    db_session.add(TradeOutcome(**base, pnl_usd=10.0, r_multiple=2.0, strategy_account_id=sa.id))
    db_session.add(TradeOutcome(**base, pnl_usd=-5.0, r_multiple=-1.0, strategy_account_id=sa.id))
    # ...and one copy-runner trade on the SAME bot with NO account → must be excluded.
    db_session.add(TradeOutcome(**base, pnl_usd=999.0, r_multiple=99.0, strategy_account_id=None))
    db_session.commit()

    r = client.get(f"/api/isolation/equity?bot_id={bot.id}", headers=auth_headers)
    assert r.status_code == 200
    body = r.json()
    assert body["total_trades"] == 2  # the 999 copy trade excluded
    assert body["cumulative_r"] == [2.0, 1.0]

    r = client.get("/api/isolation/compare", headers=auth_headers)
    assert r.status_code == 200
    strat = next(s for s in r.json()["strategies"] if s["bot_id"] == bot.id)
    stats = strat["stats"]
    assert stats["total_trades"] == 2
    assert stats["win_rate"] == 0.5
    assert stats["total_pnl_usd"] == 5.0
    assert stats["total_r"] == 1.0
