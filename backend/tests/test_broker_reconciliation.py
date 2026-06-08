"""Tests for broker statement reconciliation (Task #8).

Covers:
  - snapshot_account persists a BrokerStatement with correct totals + hash
  - snapshot_account handles broker auth failure gracefully
  - compute_discrepancies surfaces missing_close_response
  - compute_discrepancies surfaces ghost_open_position
  - _maybe_snapshot_all_accounts respects the interval (no double pull)
  - Partner endpoints scope correctly and 404 on inaccessible statements
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy.orm import sessionmaker

from app.core.crypto import encrypt
from app.db.models import (
    AccountAccessGrant,
    AccountAccessRole,
    BrokerStatement,
    SlippageRecord,
    TradingAccount,
    User,
)


@pytest.fixture(autouse=True)
def patch_session_local(db_engine):
    """Rebind SessionLocal so module-level helpers use the test DB."""
    import app.integrations.broker_reconciliation as br

    TestSession = sessionmaker(bind=db_engine, autoflush=False, autocommit=False)
    with patch.object(br, "SessionLocal", TestSession):
        yield


@pytest.fixture
def owner(db_session):
    u = User(email="owner@x.com", hashed_password="x")
    u.tradelocker_account_id = "OWNER-TL"
    u.tradelocker_acc_num = "1"
    u.tradelocker_env = "demo"
    u.tradelocker_token = encrypt("owner-token")
    db_session.add(u)
    db_session.commit()
    return u


@pytest.fixture
def trading_account(db_session, owner):
    ta = TradingAccount(
        owner_user_id=owner.id,
        label="Audit demo",
        tradelocker_account_id="2163244",
        tradelocker_acc_num="4",
        tradelocker_env="demo",
    )
    db_session.add(ta)
    db_session.commit()
    return ta


# ----------------------------------------------------------------------- #
# snapshot_account
# ----------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_snapshot_account_persists_with_totals_and_hash(
    db_session, owner, trading_account
):
    from app.integrations.broker_reconciliation import snapshot_account

    fake_state = {"balance": 100.0, "projectedBalance": 102.5, "openGrossPnL": 2.5}
    fake_positions = [{"id": "p1", "side": "buy"}]
    fake_orders = []

    with patch(
        "app.core.tradelocker_client.TradeLockerClient.get_account_state",
        new=AsyncMock(return_value=fake_state),
    ), patch(
        "app.core.tradelocker_client.TradeLockerClient.get_positions",
        new=AsyncMock(return_value=fake_positions),
    ), patch(
        "app.core.tradelocker_client.TradeLockerClient.get_orders",
        new=AsyncMock(return_value=fake_orders),
    ):
        snap_id = await snapshot_account(owner.id, trading_account, db=db_session)

    assert snap_id is not None
    snap = db_session.get(BrokerStatement, snap_id)
    assert snap.balance == 100.0
    assert snap.equity == 102.5
    assert snap.open_pnl == 2.5
    assert snap.positions_count == 1
    assert snap.orders_count == 0
    assert snap.content_sha256
    assert len(snap.content_sha256) == 64  # sha256 hex


@pytest.mark.asyncio
async def test_snapshot_account_returns_none_when_owner_has_no_token(
    db_session, owner, trading_account
):
    from app.integrations.broker_reconciliation import snapshot_account
    owner.tradelocker_token = None
    db_session.commit()

    snap_id = await snapshot_account(owner.id, trading_account, db=db_session)
    assert snap_id is None


@pytest.mark.asyncio
async def test_snapshot_account_returns_none_on_broker_error(
    db_session, owner, trading_account
):
    from app.integrations.broker_reconciliation import snapshot_account
    from app.core.tradelocker_client import TradeLockerError

    async def boom(*a, **k):
        raise TradeLockerError("502 broker down")

    with patch(
        "app.core.tradelocker_client.TradeLockerClient.get_account_state", new=boom
    ):
        snap_id = await snapshot_account(owner.id, trading_account, db=db_session)
    assert snap_id is None


# ----------------------------------------------------------------------- #
# compute_discrepancies
# ----------------------------------------------------------------------- #
def _make_snapshot(
    db_session, *, owner_user_id, ta_id, tl_acct, positions=None, **kw
) -> BrokerStatement:
    snap = BrokerStatement(
        owner_user_id=owner_user_id,
        trading_account_id=ta_id,
        tradelocker_account_id=tl_acct,
        tradelocker_acc_num="4",
        tradelocker_env="demo",
        pulled_at=kw.get("pulled_at", datetime.utcnow()),
        balance=kw.get("balance", 100.0),
        positions_count=len(positions or []),
        raw_positions_json=json.dumps(positions or []),
        content_sha256="x" * 64,
    )
    db_session.add(snap)
    db_session.commit()
    return snap


def _make_record(
    db_session, *, user_id, tl_acct, **kw
) -> SlippageRecord:
    rec = SlippageRecord(
        user_id=user_id,
        strategy_name=kw.get("strategy_name", "velocity_spike"),
        account_id=tl_acct,
        symbol=kw.get("symbol", "NAS100"),
        side=kw.get("side", "buy"),
        status=kw.get("status", "closed"),
        bar_close_ts=datetime.utcnow() - timedelta(seconds=2),
        signal_ts=datetime.utcnow() - timedelta(seconds=1),
        bar_close_price=29230.0,
        expected_entry_price=29230.0,
        hard_stop_distance_pts=50.0,
        trailing_stop_distance_pts=3.0,
        early_stop_condition="momentum_stalls_3_bars",
    )
    if "broker_close_response_json" in kw:
        rec.broker_close_response_json = kw["broker_close_response_json"]
    db_session.add(rec)
    db_session.commit()
    return rec


def test_compute_discrepancies_flags_missing_close_response(
    db_session, owner, trading_account
):
    from app.integrations.broker_reconciliation import compute_discrepancies

    _make_record(
        db_session,
        user_id=owner.id,
        tl_acct="2163244",
        status="closed",
        broker_close_response_json=None,
    )
    snap = _make_snapshot(
        db_session, owner_user_id=owner.id, ta_id=trading_account.id, tl_acct="2163244"
    )

    diffs = compute_discrepancies(snap.id, db=db_session)
    assert any(d["kind"] == "missing_close_response" for d in diffs)


def test_compute_discrepancies_does_not_flag_closed_with_response(
    db_session, owner, trading_account
):
    from app.integrations.broker_reconciliation import compute_discrepancies

    _make_record(
        db_session,
        user_id=owner.id,
        tl_acct="2163244",
        status="closed",
        broker_close_response_json='{"closed": true}',
    )
    snap = _make_snapshot(
        db_session, owner_user_id=owner.id, ta_id=trading_account.id, tl_acct="2163244"
    )

    diffs = compute_discrepancies(snap.id, db=db_session)
    assert all(d["kind"] != "missing_close_response" for d in diffs)


def test_compute_discrepancies_flags_ghost_open_position(
    db_session, owner, trading_account
):
    """Broker shows positions but we have zero open records → ghost flag."""
    from app.integrations.broker_reconciliation import compute_discrepancies

    snap = _make_snapshot(
        db_session,
        owner_user_id=owner.id,
        ta_id=trading_account.id,
        tl_acct="2163244",
        positions=[{"id": "p1", "side": "buy"}],
    )
    diffs = compute_discrepancies(snap.id, db=db_session)
    assert any(d["kind"] == "ghost_open_position" for d in diffs)


def test_compute_discrepancies_missing_statement_returns_empty(db_session):
    from app.integrations.broker_reconciliation import compute_discrepancies
    assert compute_discrepancies(999999, db=db_session) == []


# ----------------------------------------------------------------------- #
# _maybe_snapshot_all_accounts — interval gating
# ----------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_maybe_snapshot_skips_account_inside_interval(
    db_session, owner, trading_account
):
    from app.integrations import broker_reconciliation as br

    # Pre-existing snapshot 5 minutes old — still inside the default 1h
    # interval, so the tick should NOT pull again.
    _make_snapshot(
        db_session,
        owner_user_id=owner.id,
        ta_id=trading_account.id,
        tl_acct="2163244",
        pulled_at=datetime.utcnow() - timedelta(minutes=5),
    )
    calls = 0

    async def fake_get_state(*a, **k):
        nonlocal calls
        calls += 1
        return {"balance": 100}

    with patch(
        "app.core.tradelocker_client.TradeLockerClient.get_account_state",
        new=fake_get_state,
    ):
        snapped = await br._maybe_snapshot_all_accounts()
    assert snapped == 0
    assert calls == 0


@pytest.mark.asyncio
async def test_maybe_snapshot_pulls_account_overdue(
    db_session, owner, trading_account
):
    """Snapshot is 2h old — older than the 1h interval, so pull again."""
    from app.integrations import broker_reconciliation as br

    _make_snapshot(
        db_session,
        owner_user_id=owner.id,
        ta_id=trading_account.id,
        tl_acct="2163244",
        pulled_at=datetime.utcnow() - timedelta(hours=2),
    )

    with patch(
        "app.core.tradelocker_client.TradeLockerClient.get_account_state",
        new=AsyncMock(return_value={"balance": 100}),
    ), patch(
        "app.core.tradelocker_client.TradeLockerClient.get_positions",
        new=AsyncMock(return_value=[]),
    ), patch(
        "app.core.tradelocker_client.TradeLockerClient.get_orders",
        new=AsyncMock(return_value=[]),
    ):
        snapped = await br._maybe_snapshot_all_accounts()
    assert snapped == 1


# ----------------------------------------------------------------------- #
# partner endpoints
# ----------------------------------------------------------------------- #
def test_list_broker_statements_scoped_to_accessible(
    client, auth_headers, db_session, owner, trading_account
):
    from app.api.users import get_or_create_user

    partner = get_or_create_user(db_session, "tester@example.com")
    grant = AccountAccessGrant(
        account_id=trading_account.id,
        grantee_user_id=partner.id,
        role=AccountAccessRole.viewer,
        granted_by_user_id=owner.id,
    )
    db_session.add(grant)
    db_session.commit()

    _make_snapshot(
        db_session,
        owner_user_id=owner.id,
        ta_id=trading_account.id,
        tl_acct="2163244",
    )

    res = client.get(
        f"/api/partner/broker-statements?account_id={trading_account.id}",
        headers=auth_headers,
    )
    assert res.status_code == 200
    body = res.json()
    assert len(body["statements"]) == 1
    assert body["statements"][0]["balance"] == 100.0


def test_get_broker_statement_404_for_stranger(
    client, auth_headers, db_session, owner, trading_account
):
    snap = _make_snapshot(
        db_session,
        owner_user_id=owner.id,
        ta_id=trading_account.id,
        tl_acct="2163244",
    )
    # tester@example.com has no grant — should get 404
    res = client.get(
        f"/api/partner/broker-statements/{snap.id}", headers=auth_headers
    )
    assert res.status_code == 404


def test_get_broker_statement_discrepancies_returns_diffs(
    client, auth_headers, db_session, owner, trading_account
):
    from app.api.users import get_or_create_user

    partner = get_or_create_user(db_session, "tester@example.com")
    grant = AccountAccessGrant(
        account_id=trading_account.id,
        grantee_user_id=partner.id,
        role=AccountAccessRole.viewer,
        granted_by_user_id=owner.id,
    )
    db_session.add(grant)
    db_session.commit()

    _make_record(
        db_session,
        user_id=owner.id,
        tl_acct="2163244",
        status="closed",
        broker_close_response_json=None,
    )
    snap = _make_snapshot(
        db_session,
        owner_user_id=owner.id,
        ta_id=trading_account.id,
        tl_acct="2163244",
    )

    res = client.get(
        f"/api/partner/broker-statements/{snap.id}/discrepancies",
        headers=auth_headers,
    )
    assert res.status_code == 200
    diffs = res.json()["discrepancies"]
    assert any(d["kind"] == "missing_close_response" for d in diffs)
