"""Tests for the partner-scoped read API (Task #5).

Covers the security boundary first — a partner can ONLY see data tied
to their accessible TradingAccounts (owned + active grants). Cross-user
data leaks are the headline risk so every list/detail endpoint gets a
positive test (returns own data) AND a negative test (404s on someone
else's record).
"""
from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest

from app.core.crypto import encrypt
from app.db.models import (
    AccountAccessGrant,
    AccountAccessRole,
    SlippageRecord,
    TradingAccount,
    User,
)


# ----------------------------------------------------------------------- #
# fixtures
# ----------------------------------------------------------------------- #
@pytest.fixture
def owner(db_session):
    """The TradingAccount owner — runs the strategy on their broker creds."""
    u = User(email="owner@x.com", hashed_password="x")
    u.tradelocker_account_id = "OWNER-TL"
    u.tradelocker_acc_num = "1"
    u.tradelocker_env = "demo"
    u.tradelocker_token = encrypt("owner-token")
    db_session.add(u)
    db_session.commit()
    return u


@pytest.fixture
def partner(db_session):
    """The auth'd partner — must match auth_headers email so the JWT
    resolves to this row."""
    from app.api.users import get_or_create_user
    u = get_or_create_user(db_session, "tester@example.com")
    return u


@pytest.fixture
def stranger(db_session):
    """A third user whose accounts the partner must NEVER see."""
    u = User(email="stranger@x.com", hashed_password="x")
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


@pytest.fixture
def stranger_account(db_session, stranger):
    ta = TradingAccount(
        owner_user_id=stranger.id,
        label="Stranger account",
        tradelocker_account_id="9999999",
        tradelocker_acc_num="1",
        tradelocker_env="demo",
    )
    db_session.add(ta)
    db_session.commit()
    return ta


@pytest.fixture
def partner_grant(db_session, owner, partner, trading_account):
    g = AccountAccessGrant(
        account_id=trading_account.id,
        grantee_user_id=partner.id,
        role=AccountAccessRole.viewer,
        granted_by_user_id=owner.id,
        allowed_instruments_csv="NAS100",
    )
    db_session.add(g)
    db_session.commit()
    return g


def _make_record(
    db_session, *, owner_user_id: int, tl_account_id: str, **kw
) -> SlippageRecord:
    rec = SlippageRecord(
        user_id=owner_user_id,
        strategy_name=kw.get("strategy_name", "velocity_spike"),
        account_id=tl_account_id,
        symbol=kw.get("symbol", "NAS100"),
        side=kw.get("side", "buy"),
        status=kw.get("status", "closed"),
        bar_close_ts=kw.get("bar_close_ts", datetime.utcnow() - timedelta(seconds=2)),
        signal_ts=kw.get("signal_ts", datetime.utcnow() - timedelta(seconds=1)),
        bar_close_price=kw.get("bar_close_price", 29230.0),
        expected_entry_price=kw.get("expected_entry_price", 29230.0),
        actual_entry_price=kw.get("actual_entry_price", 29230.3),
        entry_slippage_pts=kw.get("entry_slippage_pts", 0.3),
        hard_stop_distance_pts=50.0,
        trailing_stop_distance_pts=3.0,
        early_stop_condition="momentum_stalls_3_bars",
        exit_type=kw.get("exit_type", "trailing"),
        actual_exit_price=kw.get("actual_exit_price", 29253.0),
        real_pnl_pts=kw.get("real_pnl_pts", 22.7),
        strategy_pnl_pts=kw.get("strategy_pnl_pts", 24.0),
    )
    db_session.add(rec)
    db_session.commit()
    return rec


# ----------------------------------------------------------------------- #
# /partner/accounts
# ----------------------------------------------------------------------- #
def test_list_accounts_returns_owned_and_granted(
    client, auth_headers, db_session, owner, partner, trading_account, partner_grant
):
    res = client.get("/api/partner/accounts", headers=auth_headers)
    assert res.status_code == 200
    accounts = res.json()["accounts"]
    assert any(
        a["tradelocker_account_id"] == "2163244" and a["role"] == "viewer"
        for a in accounts
    )


def test_list_accounts_omits_strangers_accounts(
    client, auth_headers, db_session, partner, trading_account, partner_grant,
    stranger, stranger_account,
):
    res = client.get("/api/partner/accounts", headers=auth_headers)
    assert res.status_code == 200
    accounts = res.json()["accounts"]
    assert all(
        a["tradelocker_account_id"] != "9999999" for a in accounts
    )


def test_list_accounts_requires_auth(client):
    res = client.get("/api/partner/accounts")
    assert res.status_code in (401, 403)


# ----------------------------------------------------------------------- #
# /partner/slippage-records
# ----------------------------------------------------------------------- #
def test_list_slippage_records_returns_only_accessible(
    client, auth_headers, db_session, owner, partner, trading_account,
    partner_grant, stranger, stranger_account,
):
    _make_record(db_session, owner_user_id=owner.id, tl_account_id="2163244")
    _make_record(db_session, owner_user_id=stranger.id, tl_account_id="9999999")

    res = client.get("/api/partner/slippage-records", headers=auth_headers)
    assert res.status_code == 200
    body = res.json()
    accounts_seen = {r["account_id"] for r in body["records"]}
    assert accounts_seen == {"2163244"}
    assert all(r["account_id"] != "9999999" for r in body["records"])


def test_list_slippage_records_respects_account_filter(
    client, auth_headers, db_session, owner, partner, trading_account, partner_grant
):
    _make_record(db_session, owner_user_id=owner.id, tl_account_id="2163244")

    res = client.get(
        f"/api/partner/slippage-records?account_id={trading_account.id}",
        headers=auth_headers,
    )
    assert res.status_code == 200
    assert len(res.json()["records"]) == 1


def test_list_slippage_records_account_filter_rejects_inaccessible(
    client, auth_headers, db_session, partner, stranger_account,
):
    """account_id pointing at a TradingAccount the partner can't view → 404."""
    res = client.get(
        f"/api/partner/slippage-records?account_id={stranger_account.id}",
        headers=auth_headers,
    )
    assert res.status_code == 404


def test_list_slippage_records_strategy_filter(
    client, auth_headers, db_session, owner, partner, trading_account, partner_grant
):
    _make_record(
        db_session,
        owner_user_id=owner.id,
        tl_account_id="2163244",
        strategy_name="velocity_spike",
    )
    _make_record(
        db_session,
        owner_user_id=owner.id,
        tl_account_id="2163244",
        strategy_name="vwap_reversion",
    )

    res = client.get(
        "/api/partner/slippage-records?strategy_name=velocity_spike",
        headers=auth_headers,
    )
    assert res.status_code == 200
    assert all(r["strategy"] == "velocity_spike" for r in res.json()["records"])


def test_list_slippage_records_no_accounts_returns_empty(
    client, auth_headers, db_session, partner,
):
    """Partner with no access at all → empty list, not error."""
    res = client.get("/api/partner/slippage-records", headers=auth_headers)
    assert res.status_code == 200
    assert res.json()["records"] == []


# ----------------------------------------------------------------------- #
# /partner/slippage-records/{id}
# ----------------------------------------------------------------------- #
def test_get_slippage_record_returns_full_record(
    client, auth_headers, db_session, owner, partner, trading_account, partner_grant
):
    rec = _make_record(db_session, owner_user_id=owner.id, tl_account_id="2163244")
    rec.broker_fill_response_json = '{"orderId": "tl_8847"}'
    db_session.commit()

    res = client.get(
        f"/api/partner/slippage-records/{rec.id}", headers=auth_headers
    )
    assert res.status_code == 200
    body = res.json()
    # Full payload includes the raw broker JSON — the trust anchor
    assert body["broker_fill_response"] == {"orderId": "tl_8847"}
    assert body["hard_stop_distance_pts"] == 50.0


def test_get_slippage_record_404_for_strangers_record(
    client, auth_headers, db_session, partner, stranger,
):
    rec = _make_record(
        db_session, owner_user_id=stranger.id, tl_account_id="9999999"
    )
    res = client.get(
        f"/api/partner/slippage-records/{rec.id}", headers=auth_headers
    )
    assert res.status_code == 404


def test_get_slippage_record_404_for_missing_id(client, auth_headers):
    res = client.get("/api/partner/slippage-records/999999", headers=auth_headers)
    assert res.status_code == 404


# ----------------------------------------------------------------------- #
# /partner/daily-summary
# ----------------------------------------------------------------------- #
def test_daily_summary_scoped_to_accessible_account(
    client, auth_headers, db_session, owner, partner, trading_account, partner_grant
):
    _make_record(
        db_session,
        owner_user_id=owner.id,
        tl_account_id="2163244",
        actual_entry_price=29230.5,
        actual_exit_price=29253.0,
        real_pnl_pts=22.5,
        strategy_pnl_pts=24.0,
    )

    res = client.get(
        f"/api/partner/daily-summary?account_id={trading_account.id}",
        headers=auth_headers,
    )
    assert res.status_code == 200
    body = res.json()
    assert body["account_id"] == trading_account.id
    assert body["trades_closed"] == 1
    assert body["strategy_pnl_pts"] == pytest.approx(24.0)
    assert body["real_pnl_pts"] == pytest.approx(22.5)


def test_daily_summary_rejects_inaccessible_account(
    client, auth_headers, db_session, partner, stranger_account,
):
    res = client.get(
        f"/api/partner/daily-summary?account_id={stranger_account.id}",
        headers=auth_headers,
    )
    assert res.status_code == 404


# ----------------------------------------------------------------------- #
# /partner/broker-snapshot/{account_id}
# ----------------------------------------------------------------------- #
def test_broker_snapshot_returns_owner_account_state(
    client, auth_headers, db_session, owner, partner, trading_account, partner_grant
):
    fake_state = {"balance": 100.0, "openGrossPnL": 5.0}
    fake_positions = [{"id": "tl_p1", "side": "buy"}]

    with patch(
        "app.core.tradelocker_client.TradeLockerClient.get_account_state",
        new=AsyncMock(return_value=fake_state),
    ), patch(
        "app.core.tradelocker_client.TradeLockerClient.get_positions",
        new=AsyncMock(return_value=fake_positions),
    ):
        res = client.get(
            f"/api/partner/broker-snapshot/{trading_account.id}",
            headers=auth_headers,
        )
    assert res.status_code == 200
    body = res.json()
    assert body["state"] == fake_state
    assert body["positions"] == fake_positions


def test_broker_snapshot_404_for_stranger_account(
    client, auth_headers, db_session, partner, stranger_account,
):
    res = client.get(
        f"/api/partner/broker-snapshot/{stranger_account.id}",
        headers=auth_headers,
    )
    assert res.status_code == 404


def test_broker_snapshot_502_when_owner_has_no_token(
    client, auth_headers, db_session, owner, partner, trading_account, partner_grant
):
    """If the OWNER's broker token is gone (rare but possible), surface a
    502 to the partner rather than crashing."""
    owner.tradelocker_token = None
    db_session.commit()

    res = client.get(
        f"/api/partner/broker-snapshot/{trading_account.id}",
        headers=auth_headers,
    )
    assert res.status_code == 502
