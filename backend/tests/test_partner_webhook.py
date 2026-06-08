"""Tests for the partner webhook hook (Task #6).

Covers:
  - build_payload includes the right fields per event
  - HMAC signature is reproducible by the partner using the shared secret
  - _is_discord_url detection
  - emit_partner_event posts to all matching grants and skips revoked/
    expired/no-secret grants
  - retry-on-5xx behavior + permanent-4xx-no-retry
  - schedule_emit no-ops outside an event loop
"""
from __future__ import annotations

import hashlib
import hmac
import json
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from sqlalchemy.orm import sessionmaker

from app.core.crypto import encrypt
from app.db.models import (
    AccountAccessGrant,
    AccountAccessRole,
    SlippageRecord,
    TradingAccount,
    User,
)


@pytest.fixture(autouse=True)
def patch_session_local(db_engine):
    """Rebind SessionLocal so emit_partner_event's own session uses the
    test engine when callers don't pass db= through."""
    import app.integrations.partner_webhook as pw

    TestSession = sessionmaker(bind=db_engine, autoflush=False, autocommit=False)
    with patch.object(pw, "SessionLocal", TestSession):
        yield


@pytest.fixture
def owner(db_session):
    u = User(email="owner@x.com", hashed_password="x")
    db_session.add(u)
    db_session.commit()
    return u


@pytest.fixture
def partner(db_session):
    u = User(email="partner@x.com", hashed_password="x")
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


def _make_grant(
    db_session,
    *,
    account_id: int,
    grantee_id: int,
    owner_id: int,
    secret: str = "supersecret-32chars-1234567890abcdef",
    url: str = "https://partner.example.com/events",
    expires_at=None,
    revoked_at=None,
) -> AccountAccessGrant:
    grant = AccountAccessGrant(
        account_id=account_id,
        grantee_user_id=grantee_id,
        role=AccountAccessRole.viewer,
        granted_by_user_id=owner_id,
        expires_at=expires_at,
        revoked_at=revoked_at,
        partner_webhook_url=url,
        partner_webhook_secret_encrypted=encrypt(secret),
    )
    db_session.add(grant)
    db_session.commit()
    return grant


def _make_record(
    db_session, *, user_id: int, account_id: str = "2163244", **kw
) -> SlippageRecord:
    rec = SlippageRecord(
        user_id=user_id,
        strategy_name=kw.get("strategy_name", "velocity_spike"),
        account_id=account_id,
        symbol=kw.get("symbol", "NAS100"),
        side=kw.get("side", "buy"),
        status=kw.get("status", "pending"),
        bar_close_ts=kw.get("bar_close_ts", datetime.utcnow()),
        signal_ts=kw.get("signal_ts", datetime.utcnow()),
        bar_close_price=kw.get("bar_close_price", 29230.0),
        expected_entry_price=kw.get("expected_entry_price", 29230.0),
        hard_stop_distance_pts=kw.get("hard_stop_distance_pts", 50.0),
        trailing_stop_distance_pts=kw.get("trailing_stop_distance_pts", 3.0),
        early_stop_condition=kw.get("early_stop_condition", "momentum_stalls_3_bars"),
    )
    db_session.add(rec)
    db_session.commit()
    return rec


# ----------------------------------------------------------------------- #
# payload builders
# ----------------------------------------------------------------------- #
def test_build_payload_signal_carries_audit_fields(db_session, owner):
    from app.integrations.partner_webhook import build_payload
    rec = _make_record(db_session, user_id=owner.id)
    body = build_payload("signal", rec)

    assert body["event"] == "signal"
    assert body["slippage_record_id"] == rec.id
    assert body["strategy"] == "velocity_spike"
    assert body["symbol"] == "NAS100"
    assert body["expected_entry_price"] == 29230.0
    assert body["hard_stop_distance_pts"] == 50.0
    assert body["trailing_stop_distance_pts"] == 3.0
    assert body["early_stop_condition"] == "momentum_stalls_3_bars"


def test_build_payload_fill_includes_slippage_and_raw_broker_response(db_session, owner):
    from app.integrations.partner_webhook import build_payload
    rec = _make_record(db_session, user_id=owner.id)
    rec.actual_entry_price = 29230.3
    rec.entry_slippage_pts = 0.3
    rec.total_latency_ms = 250
    rec.broker_fill_response_json = json.dumps({"orderId": "tl_8847"})
    db_session.commit()

    body = build_payload("fill", rec)
    assert body["actual_entry_price"] == 29230.3
    assert body["entry_slippage_pts"] == 0.3
    assert body["total_latency_ms"] == 250
    assert body["broker_raw_response"] == {"orderId": "tl_8847"}


def test_build_payload_close_includes_pnl_pair(db_session, owner):
    from app.integrations.partner_webhook import build_payload
    rec = _make_record(db_session, user_id=owner.id)
    rec.exit_type = "trailing"
    rec.actual_exit_price = 29253.0
    rec.real_pnl_pts = 22.5
    rec.strategy_pnl_pts = 24.0
    rec.slippage_total_pts = 1.5
    rec.slippage_total_dollars = 0.15
    db_session.commit()

    body = build_payload("close", rec)
    assert body["exit_type"] == "trailing"
    assert body["real_pnl_pts"] == 22.5
    assert body["strategy_pnl_pts"] == 24.0
    assert body["slippage_total_pts"] == 1.5
    assert body["slippage_total_dollars"] == 0.15


# ----------------------------------------------------------------------- #
# HMAC signature is reproducible
# ----------------------------------------------------------------------- #
def test_signature_matches_independent_hmac_compute():
    from app.integrations.partner_webhook import _signature

    secret = "supersecret-key"
    body = b'{"event":"signal"}'
    expected = "sha256=" + hmac.new(
        secret.encode(), body, hashlib.sha256
    ).hexdigest()
    assert _signature(secret, body) == expected


def test_is_discord_url_detection():
    from app.integrations.partner_webhook import _is_discord_url
    assert _is_discord_url("https://discord.com/api/webhooks/123/abc") is True
    assert _is_discord_url("https://discordapp.com/api/webhooks/123/abc") is True
    assert _is_discord_url("https://partner.example.com/events") is False
    assert _is_discord_url("http://discord.com/api/webhooks/123/abc") is False


# ----------------------------------------------------------------------- #
# dispatcher — emit_partner_event
# ----------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_emit_partner_event_posts_to_active_grant(
    db_session, owner, partner, trading_account
):
    from app.integrations.partner_webhook import emit_partner_event

    _make_grant(
        db_session,
        account_id=trading_account.id,
        grantee_id=partner.id,
        owner_id=owner.id,
    )
    rec = _make_record(db_session, user_id=owner.id)

    captured: dict = {}

    async def fake_post(self, url, content, headers):
        captured["url"] = url
        captured["content"] = content
        captured["headers"] = dict(headers)
        return httpx.Response(200, request=httpx.Request("POST", url))

    with patch("httpx.AsyncClient.post", new=fake_post):
        n = await emit_partner_event("signal", rec.id, db=db_session)

    assert n == 1
    assert captured["url"] == "https://partner.example.com/events"
    assert captured["headers"]["X-Event"] == "signal"
    assert captured["headers"]["X-Signature"].startswith("sha256=")
    assert captured["headers"]["X-Timestamp"].isdigit()
    body = json.loads(captured["content"])
    assert body["slippage_record_id"] == rec.id
    assert body["event"] == "signal"


@pytest.mark.asyncio
async def test_emit_partner_event_skips_revoked_grant(
    db_session, owner, partner, trading_account
):
    from app.integrations.partner_webhook import emit_partner_event

    _make_grant(
        db_session,
        account_id=trading_account.id,
        grantee_id=partner.id,
        owner_id=owner.id,
        revoked_at=datetime.utcnow(),
    )
    rec = _make_record(db_session, user_id=owner.id)

    n = await emit_partner_event("signal", rec.id, db=db_session)
    assert n == 0


@pytest.mark.asyncio
async def test_emit_partner_event_skips_expired_grant(
    db_session, owner, partner, trading_account
):
    from app.integrations.partner_webhook import emit_partner_event

    _make_grant(
        db_session,
        account_id=trading_account.id,
        grantee_id=partner.id,
        owner_id=owner.id,
        expires_at=datetime.utcnow() - timedelta(hours=1),
    )
    rec = _make_record(db_session, user_id=owner.id)

    n = await emit_partner_event("signal", rec.id, db=db_session)
    assert n == 0


@pytest.mark.asyncio
async def test_emit_partner_event_skips_grant_with_no_url(
    db_session, owner, partner, trading_account
):
    from app.integrations.partner_webhook import emit_partner_event

    grant = _make_grant(
        db_session,
        account_id=trading_account.id,
        grantee_id=partner.id,
        owner_id=owner.id,
    )
    grant.partner_webhook_url = None
    db_session.commit()
    rec = _make_record(db_session, user_id=owner.id)

    n = await emit_partner_event("signal", rec.id, db=db_session)
    assert n == 0


@pytest.mark.asyncio
async def test_emit_partner_event_uses_discord_payload_for_discord_url(
    db_session, owner, partner, trading_account
):
    from app.integrations.partner_webhook import emit_partner_event

    _make_grant(
        db_session,
        account_id=trading_account.id,
        grantee_id=partner.id,
        owner_id=owner.id,
        url="https://discord.com/api/webhooks/123/abc",
    )
    rec = _make_record(db_session, user_id=owner.id)

    captured: dict = {}

    async def fake_post(self, url, content, headers):
        captured["url"] = url
        captured["content"] = content
        captured["headers"] = dict(headers)
        return httpx.Response(200, request=httpx.Request("POST", url))

    with patch("httpx.AsyncClient.post", new=fake_post):
        await emit_partner_event("signal", rec.id, db=db_session)

    body = json.loads(captured["content"])
    # Discord format uses `embeds`, not the standard event/slippage keys
    assert "embeds" in body
    assert body["embeds"][0]["title"].startswith("[SIGNAL]")
    # Discord webhooks do NOT receive the HMAC signature header — Discord
    # rejects custom X-* headers anyway.
    assert "X-Signature" not in captured["headers"]


@pytest.mark.asyncio
async def test_emit_partner_event_retries_on_5xx(
    db_session, owner, partner, trading_account
):
    from app.integrations.partner_webhook import emit_partner_event
    import app.integrations.partner_webhook as pw

    _make_grant(
        db_session,
        account_id=trading_account.id,
        grantee_id=partner.id,
        owner_id=owner.id,
    )
    rec = _make_record(db_session, user_id=owner.id)

    attempts = 0

    async def fake_post(self, url, content, headers):
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            return httpx.Response(500, request=httpx.Request("POST", url))
        return httpx.Response(200, request=httpx.Request("POST", url))

    # Shrink retry delays so the test doesn't take 21 seconds.
    with patch.object(pw, "_RETRY_DELAYS_SEC", (0, 0, 0)), patch(
        "httpx.AsyncClient.post", new=fake_post
    ):
        n = await emit_partner_event("signal", rec.id, db=db_session)

    assert n == 1
    assert attempts == 3


@pytest.mark.asyncio
async def test_emit_partner_event_no_retry_on_4xx(
    db_session, owner, partner, trading_account
):
    """4xx errors are permanent — bad URL, invalid Discord token, etc.
    Retrying just bangs on the same wall."""
    from app.integrations.partner_webhook import emit_partner_event
    import app.integrations.partner_webhook as pw

    _make_grant(
        db_session,
        account_id=trading_account.id,
        grantee_id=partner.id,
        owner_id=owner.id,
    )
    rec = _make_record(db_session, user_id=owner.id)

    attempts = 0

    async def fake_post(self, url, content, headers):
        nonlocal attempts
        attempts += 1
        return httpx.Response(404, request=httpx.Request("POST", url))

    with patch.object(pw, "_RETRY_DELAYS_SEC", (0, 0, 0)), patch(
        "httpx.AsyncClient.post", new=fake_post
    ):
        n = await emit_partner_event("signal", rec.id, db=db_session)

    assert n == 1   # counted as one delivery attempt
    assert attempts == 1  # no retry on 4xx


@pytest.mark.asyncio
async def test_emit_partner_event_missing_record_returns_zero(db_session):
    from app.integrations.partner_webhook import emit_partner_event
    n = await emit_partner_event("signal", 999999, db=db_session)
    assert n == 0


@pytest.mark.asyncio
async def test_emit_partner_event_unknown_event_returns_zero(db_session, owner):
    from app.integrations.partner_webhook import emit_partner_event
    rec = _make_record(db_session, user_id=owner.id)
    n = await emit_partner_event("not_a_thing", rec.id, db=db_session)
    assert n == 0


# ----------------------------------------------------------------------- #
# multi-grant dispatch
# ----------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_emit_partner_event_fans_to_multiple_active_grants(
    db_session, owner, trading_account
):
    """Two partners with active grants on the same account both receive
    the event."""
    from app.integrations.partner_webhook import emit_partner_event

    p1 = User(email="p1@x.com", hashed_password="x")
    p2 = User(email="p2@x.com", hashed_password="x")
    db_session.add_all([p1, p2])
    db_session.commit()

    _make_grant(
        db_session,
        account_id=trading_account.id,
        grantee_id=p1.id,
        owner_id=owner.id,
        url="https://p1.example.com/events",
    )
    _make_grant(
        db_session,
        account_id=trading_account.id,
        grantee_id=p2.id,
        owner_id=owner.id,
        url="https://p2.example.com/events",
    )
    rec = _make_record(db_session, user_id=owner.id)

    urls_called: list[str] = []

    async def fake_post(self, url, content, headers):
        urls_called.append(url)
        return httpx.Response(200, request=httpx.Request("POST", url))

    with patch("httpx.AsyncClient.post", new=fake_post):
        n = await emit_partner_event("signal", rec.id, db=db_session)

    assert n == 2
    assert set(urls_called) == {
        "https://p1.example.com/events",
        "https://p2.example.com/events",
    }


# ----------------------------------------------------------------------- #
# schedule_emit — sync fire-and-forget
# ----------------------------------------------------------------------- #
def test_schedule_emit_noop_without_event_loop():
    """Should not raise when called outside an async context."""
    from app.integrations.partner_webhook import schedule_emit
    schedule_emit("signal", 1)  # no loop running — must be a no-op


@pytest.mark.asyncio
async def test_schedule_emit_creates_task_when_loop_present():
    from app.integrations.partner_webhook import schedule_emit
    with patch(
        "app.integrations.partner_webhook.emit_partner_event",
        new=AsyncMock(return_value=0),
    ) as mock_emit:
        schedule_emit("signal", 42)
        # Give the scheduled task a chance to run
        import asyncio
        await asyncio.sleep(0)
        mock_emit.assert_called_once_with("signal", 42)
