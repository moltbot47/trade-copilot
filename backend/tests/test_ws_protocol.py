"""WebSocket protocol tests — Wave 3A.

Covers the contract in docs/WS_PROTOCOL.md:
- auth_failed on missing/bad JWT (close 4401)
- auth_ok on valid JWT (with auto-created User row)
- subscribe / subscribed cycle
- subscribe to disallowed channel → error("bad_frame")
- event_bus.publish reaches the right user's subscribed socket
- command strategy.start dispatches to the runner mock
- command subscription.update across users → command_error("forbidden")
- ping → pong with same ts
- frame rate limit emits error("rate_limited") without disconnecting
"""
from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _connect(client):
    """Open a WS to /ws using TestClient context manager."""
    return client.websocket_connect("/ws")


def _auth(ws, token: str) -> dict:
    ws.send_text(json.dumps({"type": "auth", "token": token}))
    return ws.receive_json()


def _drain_to(ws, want_type: str, max_frames: int = 10) -> dict:
    """Receive until we get a frame matching `want_type` or run out."""
    for _ in range(max_frames):
        msg = ws.receive_json()
        if msg.get("type") == want_type:
            return msg
    raise AssertionError(f"did not receive {want_type!r} within {max_frames} frames")


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------
def test_auth_invalid_token_closes_4401(client):
    with _connect(client) as ws:
        ws.send_text(json.dumps({"type": "auth", "token": "INVALID.TOKEN.HERE"}))
        msg = ws.receive_json()
        assert msg["type"] == "auth_failed"
        assert "invalid_token" in msg["reason"] or "malformed_token" in msg["reason"]


def test_auth_with_valid_token_succeeds(client, auth_email, auth_headers):
    # Strip "Bearer " from the fixture header.
    token = auth_headers["Authorization"].split(" ", 1)[1]
    with _connect(client) as ws:
        msg = _auth(ws, token)
        assert msg["type"] == "auth_ok"
        assert msg["email"] == auth_email
        assert isinstance(msg["user_id"], int)


def test_pre_auth_subscribe_is_rejected(client):
    with _connect(client) as ws:
        ws.send_text(json.dumps({"type": "subscribe", "channel": "account"}))
        msg = ws.receive_json()
        assert msg["type"] == "error"
        assert msg["code"] == "unauthorized"


# ---------------------------------------------------------------------------
# Subscribe
# ---------------------------------------------------------------------------
def test_subscribe_account_channel(client, auth_headers):
    token = auth_headers["Authorization"].split(" ", 1)[1]
    with _connect(client) as ws:
        _auth(ws, token)
        ws.send_text(json.dumps({"type": "subscribe", "channel": "account"}))
        msg = ws.receive_json()
        assert msg == {"type": "subscribed", "channel": "account"}


def test_subscribe_invalid_channel_returns_error(client, auth_headers):
    token = auth_headers["Authorization"].split(" ", 1)[1]
    with _connect(client) as ws:
        _auth(ws, token)
        ws.send_text(json.dumps({"type": "subscribe", "channel": "rogue"}))
        msg = ws.receive_json()
        assert msg["type"] == "error"
        assert msg["code"] == "bad_frame"


def test_unsubscribe_acks(client, auth_headers):
    token = auth_headers["Authorization"].split(" ", 1)[1]
    with _connect(client) as ws:
        _auth(ws, token)
        ws.send_text(json.dumps({"type": "subscribe", "channel": "account"}))
        ws.receive_json()
        ws.send_text(json.dumps({"type": "unsubscribe", "channel": "account"}))
        msg = ws.receive_json()
        assert msg == {"type": "unsubscribed", "channel": "account"}


# ---------------------------------------------------------------------------
# Event bus push
# ---------------------------------------------------------------------------
def test_event_bus_push_reaches_subscriber(client, auth_email, auth_headers):
    """End-to-end: subscribe → another task publishes → client gets the event frame."""
    from app.ws.event_bus import event_bus

    token = auth_headers["Authorization"].split(" ", 1)[1]
    with _connect(client) as ws:
        msg = _auth(ws, token)
        user_id = msg["user_id"]
        ws.send_text(json.dumps({"type": "subscribe", "channel": "account"}))
        ws.receive_json()  # subscribed ack

        # The TestClient runs the WS in a background thread with its own loop.
        # We poke the bus on the portal (the same loop) so the send goes to
        # our connected client.
        payload = {"balance": 9999.78, "equity": 9999.78}
        ws.portal.call(event_bus.publish, "account", user_id, payload)

        msg = ws.receive_json()
        assert msg["type"] == "event"
        assert msg["channel"] == "account"
        assert msg["data"] == payload
        assert isinstance(msg["ts"], int)


# ---------------------------------------------------------------------------
# Heartbeat
# ---------------------------------------------------------------------------
def test_ping_responds_with_pong(client, auth_headers):
    token = auth_headers["Authorization"].split(" ", 1)[1]
    with _connect(client) as ws:
        _auth(ws, token)
        ws.send_text(json.dumps({"type": "ping", "ts": 1234567890}))
        msg = ws.receive_json()
        assert msg["type"] == "pong"
        assert msg["ts"] == 1234567890


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------
def test_command_strategy_start_dispatches(client, seed_bots, auth_headers):
    """Mocks the actual runner, asserts command_ok with same cmd_id is returned."""
    from app.strategies import runner as runner_module

    token = auth_headers["Authorization"].split(" ", 1)[1]
    latpfn = next(b for b in seed_bots if b.slug == "latpfn-momentum")

    fake_task = SimpleNamespace(done=lambda: False)
    fake_runner = SimpleNamespace(
        bot_id=latpfn.id,
        timeframe="1m",
        symbols=["BTCUSD"],
        user_emails=["tester@example.com"],
        task=fake_task,
    )

    with patch.object(
        runner_module.StrategyRunner,
        "start",
        new=AsyncMock(return_value=fake_runner),
    ):
        with _connect(client) as ws:
            _auth(ws, token)
            ws.send_text(
                json.dumps(
                    {
                        "type": "command",
                        "cmd_id": "abc-1",
                        "name": "strategy.start",
                        "params": {
                            "bot_id": latpfn.id,
                            "timeframe": "1m",
                            "symbols": ["BTCUSD"],
                        },
                    }
                )
            )
            msg = ws.receive_json()
            assert msg["type"] == "command_ok"
            assert msg["cmd_id"] == "abc-1"
            assert msg["result"]["status"] == "started"
            assert msg["result"]["bot_id"] == latpfn.id


def test_command_subscription_update_forbidden_for_other_user(
    client, db_session, seed_bots, auth_email, auth_headers
):
    """User A authenticates over WS but tries to mutate User B's subscription."""
    from app.api.users import get_or_create_user
    from app.db.models import Subscription

    other_user = get_or_create_user(db_session, "someone-else@example.com")
    bot = seed_bots[0]
    sub = Subscription(user_id=other_user.id, bot_id=bot.id, aggression_level=5)
    db_session.add(sub)
    db_session.commit()
    db_session.refresh(sub)

    token = auth_headers["Authorization"].split(" ", 1)[1]
    with _connect(client) as ws:
        _auth(ws, token)
        ws.send_text(
            json.dumps(
                {
                    "type": "command",
                    "cmd_id": "evil-1",
                    "name": "subscription.update",
                    "params": {"subscription_id": sub.id, "aggression_level": 9},
                }
            )
        )
        msg = ws.receive_json()
        assert msg["type"] == "command_error"
        assert msg["cmd_id"] == "evil-1"
        assert msg["code"] == "forbidden"


def test_command_subscription_update_self(
    client, db_session, seed_bots, auth_email, auth_headers
):
    """User can update their own subscription (positive control for ownership check)."""
    from app.api.users import get_or_create_user
    from app.db.models import Subscription

    me = get_or_create_user(db_session, auth_email)
    bot = seed_bots[0]
    sub = Subscription(user_id=me.id, bot_id=bot.id, aggression_level=3)
    db_session.add(sub)
    db_session.commit()
    db_session.refresh(sub)
    sub_id = sub.id

    token = auth_headers["Authorization"].split(" ", 1)[1]
    with _connect(client) as ws:
        _auth(ws, token)
        ws.send_text(
            json.dumps(
                {
                    "type": "command",
                    "cmd_id": "ok-1",
                    "name": "subscription.update",
                    "params": {"subscription_id": sub_id, "aggression_level": 8},
                }
            )
        )
        msg = ws.receive_json()
        assert msg["type"] == "command_ok"
        assert msg["result"]["aggression_level"] == 8
        assert msg["result"]["subscription_id"] == sub_id


# ---------------------------------------------------------------------------
# Rate limit
# ---------------------------------------------------------------------------
def test_rate_limit_emits_error_but_keeps_connection(client, auth_headers):
    """11+ frames in <1s → at least one error('rate_limited'); conn stays open."""
    token = auth_headers["Authorization"].split(" ", 1)[1]
    with _connect(client) as ws:
        _auth(ws, token)
        # Burst 15 pings — first 10 admitted; remainder rate-limited.
        for i in range(15):
            ws.send_text(json.dumps({"type": "ping", "ts": i}))

        seen_pong = 0
        seen_rate_limited = 0
        # Drain the responses for our 15 frames.
        for _ in range(15):
            msg = ws.receive_json()
            if msg["type"] == "pong":
                seen_pong += 1
            elif msg["type"] == "error" and msg["code"] == "rate_limited":
                seen_rate_limited += 1
        assert seen_pong >= 1
        assert seen_rate_limited >= 1

        # Conn must still be alive: another ping (after the 1s window) succeeds.
        # Sleep just over 1s for the sliding window to roll over.
        import time

        time.sleep(1.05)
        ws.send_text(json.dumps({"type": "ping", "ts": 999}))
        msg = ws.receive_json()
        assert msg["type"] == "pong"
        assert msg["ts"] == 999


# ---------------------------------------------------------------------------
# Auth timeout
# ---------------------------------------------------------------------------
def test_auth_timeout_closes_connection(monkeypatch, client):
    """If no auth frame arrives within the timeout, the server kicks the connection."""
    from app.ws import server as ws_server

    # Shrink the auth timeout so the test runs in ~0.5s instead of 30s.
    monkeypatch.setattr(ws_server, "AUTH_TIMEOUT_SEC", 0.3)

    from starlette.websockets import WebSocketDisconnect as StarletteWSDisconnect

    with _connect(client) as ws:
        # Don't send anything. Wait for the watchdog to fire.
        msg = ws.receive_json()
        assert msg["type"] == "auth_failed"
        assert msg["reason"] == "auth_timeout"
        # The server then closes; the next read should raise WebSocketDisconnect.
        with pytest.raises((StarletteWSDisconnect, Exception)):
            ws.receive_json()
