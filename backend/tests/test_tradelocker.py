"""TradeLocker connect + account tests with the real client mocked."""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest


_FAKE_AUTH = {
    "access_token": "fake-jwt-access-token",
    "refresh_token": "fake-jwt-refresh-token",
    "expire_date": "2099-01-01T00:00:00Z",
    "account_id": "2163244",
    "acc_num": "4",
    "balance": 10_000.0,
    "currency": "USD",
    "all_accounts": [{"id": "2163244", "accNum": "4"}],
}

_FAKE_STATE = {
    "balance": 10_000.0,
    "projectedBalance": 10_125.5,
    "availableFunds": 9_500.0,
    "openGrossPnL": 125.5,
    "todayNet": 12.3,
    "positionsCount": 2,
}


def test_connect_without_auth_header_returns_401(client):
    res = client.post(
        "/api/tradelocker/connect",
        json={
            "email": "x@example.com",
            "password": "pw",
            "server": "GENFX",
            "env": "demo",
        },
    )
    assert res.status_code == 401


def test_connect_success_persists_state(client, auth_headers):
    with patch(
        "app.api.tradelocker.TradeLockerClient.authenticate",
        new=AsyncMock(return_value=_FAKE_AUTH),
    ):
        res = client.post(
            "/api/tradelocker/connect",
            headers=auth_headers,
            json={
                "email": "trader@example.com",
                "password": "secret123",
                "server": "GENFX",
                "env": "demo",
            },
        )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["status"] == "connected"
    assert body["detail"] == "2163244"


def test_connect_auth_error_returns_400(client, auth_headers):
    from app.core.tradelocker_client import TradeLockerError

    with patch(
        "app.api.tradelocker.TradeLockerClient.authenticate",
        new=AsyncMock(side_effect=TradeLockerError("incorrect email or password")),
    ):
        res = client.post(
            "/api/tradelocker/connect",
            headers=auth_headers,
            json={
                "email": "trader@example.com",
                "password": "wrong",
                "server": "GENFX",
                "env": "demo",
            },
        )
    assert res.status_code == 400
    assert "Invalid" in res.json()["detail"]


def test_connect_unknown_server_message(client, auth_headers):
    from app.core.tradelocker_client import TradeLockerError

    with patch(
        "app.api.tradelocker.TradeLockerClient.authenticate",
        new=AsyncMock(side_effect=TradeLockerError("server exists?")),
    ):
        res = client.post(
            "/api/tradelocker/connect",
            headers=auth_headers,
            json={
                "email": "trader@example.com",
                "password": "secret",
                "server": "UNKNOWN",
                "env": "demo",
            },
        )
    assert res.status_code == 400
    assert "GENFX" in res.json()["detail"]


def test_account_state_disconnected_returns_connected_false(client, auth_headers):
    res = client.get("/api/tradelocker/account", headers=auth_headers)
    assert res.status_code == 200
    body = res.json()
    assert body["connected"] is False
    assert body["env"] in ("demo", "live")


def test_account_state_connected_maps_fields(client, auth_headers):
    # First connect
    with patch(
        "app.api.tradelocker.TradeLockerClient.authenticate",
        new=AsyncMock(return_value=_FAKE_AUTH),
    ):
        client.post(
            "/api/tradelocker/connect",
            headers=auth_headers,
            json={
                "email": "trader@example.com",
                "password": "secret",
                "server": "GENFX",
                "env": "demo",
            },
        )

    with patch(
        "app.api.tradelocker.TradeLockerClient.get_account_state",
        new=AsyncMock(return_value=_FAKE_STATE),
    ):
        res = client.get("/api/tradelocker/account", headers=auth_headers)
    assert res.status_code == 200
    body = res.json()
    assert body["connected"] is True
    assert body["balance"] == pytest.approx(10_000.0)
    assert body["equity"] == pytest.approx(10_125.5)
    assert body["available_funds"] == pytest.approx(9_500.0)
    assert body["open_pnl"] == pytest.approx(125.5)
    assert body["positions_count"] == 2
    assert body["account_id"] == "2163244"


def test_account_state_with_lookup_failure_still_returns_connected(client, auth_headers):
    from app.core.tradelocker_client import TradeLockerError

    with patch(
        "app.api.tradelocker.TradeLockerClient.authenticate",
        new=AsyncMock(return_value=_FAKE_AUTH),
    ):
        client.post(
            "/api/tradelocker/connect",
            headers=auth_headers,
            json={
                "email": "trader@example.com",
                "password": "secret",
                "server": "GENFX",
                "env": "demo",
            },
        )

    with patch(
        "app.api.tradelocker.TradeLockerClient.get_account_state",
        new=AsyncMock(side_effect=TradeLockerError("upstream 500")),
    ):
        res = client.get("/api/tradelocker/account", headers=auth_headers)
    # Falls back to "connected, but no state numbers"
    assert res.status_code == 200
    body = res.json()
    assert body["connected"] is True
    assert body["balance"] is None


def test_connect_strips_whitespace_from_server(client, auth_headers):
    """Real users copy-paste with trailing/leading whitespace and the
    `^[A-Za-z0-9_-]+$` server regex used to reject those payloads with a
    422 and no useful UI hint. The schema's field_validator now strips
    before the regex fires."""
    with patch(
        "app.api.tradelocker.TradeLockerClient.authenticate",
        new=AsyncMock(return_value=_FAKE_AUTH),
    ):
        res = client.post(
            "/api/tradelocker/connect",
            headers=auth_headers,
            json={
                "email": "trader@example.com",
                "password": "secret",
                "server": "  GENFX  ",  # ← would have been rejected pre-fix
                "env": "demo",
            },
        )
    assert res.status_code in (200, 201), res.text
    body = res.json()
    assert body["status"] == "connected"


def test_connect_strips_whitespace_from_password(client, auth_headers):
    """Same defense applied to password. The broker still validates the
    actual creds upstream; we just clean up the wire payload."""
    with patch(
        "app.api.tradelocker.TradeLockerClient.authenticate",
        new=AsyncMock(return_value=_FAKE_AUTH),
    ) as auth_mock:
        client.post(
            "/api/tradelocker/connect",
            headers=auth_headers,
            json={
                "email": "trader@example.com",
                "password": "  secret  ",
                "server": "GENFX",
                "env": "demo",
            },
        )
    # The broker call should receive the stripped password, not the padded one.
    auth_mock.assert_awaited_once()
    _, kwargs = auth_mock.await_args
    args = auth_mock.await_args.args
    # authenticate(email, password, server) is positional in the runner
    assert args[1] == "secret"
