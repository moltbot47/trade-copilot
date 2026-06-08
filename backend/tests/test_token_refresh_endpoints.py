"""Auto-refresh behavior on /api/tradelocker/{accounts,switch-account}
and the new /api/tradelocker/refresh endpoint.

These exist because the picker silently broke for users whose access token
had aged out — even though their refresh token was still valid. They now
self-heal instead of forcing a full Genesis FX re-auth.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from app.core.crypto import encrypt
from app.core.tradelocker_client import TradeLockerError
from app.db.models import User


def _link(db_session, email: str) -> User:
    u = db_session.query(User).filter(User.email == email).first()
    u.tradelocker_token = encrypt("expired-access")
    u.tradelocker_refresh_token = encrypt("good-refresh")
    u.tradelocker_env = "demo"
    u.tradelocker_account_id = "728340"
    u.tradelocker_acc_num = "1"
    db_session.commit()
    return u


_ACCOUNTS = [
    {"id": "728340", "accNum": "1", "name": "Genesis Demo", "accountBalance": 8.69, "currency": "USD"},
]


def test_accounts_succeeds_when_token_valid(
    client, auth_headers, auth_email, db_session
):
    _link(db_session, auth_email)
    with patch(
        "app.api.tradelocker.TradeLockerClient.list_all_accounts",
        new=AsyncMock(return_value=_ACCOUNTS),
    ) as p:
        r = client.get("/api/tradelocker/accounts", headers=auth_headers)
    assert r.status_code == 200
    assert r.json()["accounts"][0]["account_id"] == "728340"
    p.assert_awaited_once()


def test_accounts_auto_refreshes_on_401(
    client, auth_headers, auth_email, db_session
):
    """First broker call 401s, refresh succeeds, second call succeeds — the
    endpoint returns accounts without surfacing the expiry to the user."""
    _link(db_session, auth_email)

    mock_list = AsyncMock(
        side_effect=[
            TradeLockerError("unauthorized (401) - token expired or invalid"),
            _ACCOUNTS,
        ]
    )
    with patch(
        "app.api.tradelocker.TradeLockerClient.list_all_accounts", new=mock_list
    ), patch(
        "app.api.tradelocker.refresh_user_token",
        new=AsyncMock(return_value="fresh-access"),
    ) as mock_refresh:
        r = client.get("/api/tradelocker/accounts", headers=auth_headers)

    assert r.status_code == 200, r.text
    assert r.json()["accounts"][0]["account_id"] == "728340"
    mock_refresh.assert_awaited_once()
    assert mock_list.await_count == 2  # initial + retry-after-refresh
    # Retry used the fresh token, not the expired one.
    assert mock_list.await_args_list[1].args[0] == "fresh-access"


def test_accounts_returns_reconnect_required_when_refresh_fails(
    client, auth_headers, auth_email, db_session
):
    _link(db_session, auth_email)
    with patch(
        "app.api.tradelocker.TradeLockerClient.list_all_accounts",
        new=AsyncMock(side_effect=TradeLockerError("unauthorized (401)")),
    ), patch(
        "app.api.tradelocker.refresh_user_token",
        new=AsyncMock(return_value=None),
    ):
        r = client.get("/api/tradelocker/accounts", headers=auth_headers)
    assert r.status_code == 401
    assert r.json()["detail"] == "reconnect_required"


def test_accounts_non_401_error_returns_502(
    client, auth_headers, auth_email, db_session
):
    """A 5xx from the broker (network/down) is NOT an auth failure — must
    not trigger a refresh, must surface as 502."""
    _link(db_session, auth_email)
    refresh = AsyncMock(return_value="should-not-be-called")
    with patch(
        "app.api.tradelocker.TradeLockerClient.list_all_accounts",
        new=AsyncMock(side_effect=TradeLockerError("503 broker down")),
    ), patch("app.api.tradelocker.refresh_user_token", new=refresh):
        r = client.get("/api/tradelocker/accounts", headers=auth_headers)
    assert r.status_code == 502
    refresh.assert_not_awaited()


def test_explicit_refresh_endpoint_success(
    client, auth_headers, auth_email, db_session
):
    _link(db_session, auth_email)
    with patch(
        "app.api.tradelocker.refresh_user_token",
        new=AsyncMock(return_value="fresh-access"),
    ):
        r = client.post("/api/tradelocker/refresh", headers=auth_headers)
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_explicit_refresh_endpoint_requires_reconnect_when_refresh_dead(
    client, auth_headers, auth_email, db_session
):
    _link(db_session, auth_email)
    with patch(
        "app.api.tradelocker.refresh_user_token",
        new=AsyncMock(return_value=None),
    ):
        r = client.post("/api/tradelocker/refresh", headers=auth_headers)
    assert r.status_code == 401
    assert r.json()["detail"] == "reconnect_required"


def test_explicit_refresh_endpoint_requires_linked_broker(
    client, auth_headers, auth_email, db_session
):
    # User exists (auth_headers created them) but has no broker linked.
    r = client.post("/api/tradelocker/refresh", headers=auth_headers)
    assert r.status_code == 400
    assert "no broker linked" in r.json()["detail"]
