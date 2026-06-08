"""Tests for the partner daily summary cron (Task #9)."""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from unittest.mock import patch

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
    """Rebind SessionLocal so module-level helpers use the test DB."""
    import app.integrations.partner_daily_summary as pds
    import app.monitoring.slippage_tracker as st

    TestSession = sessionmaker(bind=db_engine, autoflush=False, autocommit=False)
    with patch.object(pds, "SessionLocal", TestSession), patch.object(
        st, "SessionLocal", TestSession
    ):
        # Also clear the in-process dedup set between tests.
        pds._published.clear()
        yield
        pds._published.clear()


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


def _make_grant(db_session, *, account_id, grantee_id, owner_id, url, secret="webhook-secret"):
    g = AccountAccessGrant(
        account_id=account_id,
        grantee_user_id=grantee_id,
        role=AccountAccessRole.viewer,
        granted_by_user_id=owner_id,
        partner_webhook_url=url,
        partner_webhook_secret_encrypted=encrypt(secret),
    )
    db_session.add(g)
    db_session.commit()
    return g


def _make_closed_record(db_session, *, owner_user_id, tl_account_id, **kw):
    yesterday = datetime.utcnow() - timedelta(days=1)
    rec = SlippageRecord(
        user_id=owner_user_id,
        strategy_name="velocity_spike",
        account_id=tl_account_id,
        symbol="NAS100",
        side="buy",
        status="closed",
        bar_close_ts=yesterday,
        signal_ts=yesterday,
        bar_close_price=29230.0,
        expected_entry_price=29230.0,
        actual_entry_price=29230.5,
        entry_slippage_pts=0.5,
        hard_stop_distance_pts=50.0,
        trailing_stop_distance_pts=3.0,
        early_stop_condition="momentum_stalls_3_bars",
        exit_type="trailing",
        actual_exit_price=29253.0,
        real_pnl_pts=22.5,
        strategy_pnl_pts=24.0,
        slippage_total_pts=1.5,
        slippage_total_dollars=0.15,
        total_latency_ms=240,
    )
    # Force created_at into yesterday so compute_daily_summary's day-window
    # filter catches it.
    rec.created_at = yesterday
    db_session.add(rec)
    db_session.commit()
    return rec


# ----------------------------------------------------------------------- #
# embed + payload builders
# ----------------------------------------------------------------------- #
def test_discord_embed_includes_pnl_and_friction_fields():
    from app.integrations.partner_daily_summary import _build_discord_embed

    summary = {
        "trades_closed": 3,
        "signals_emitted": 5,
        "signals_rejected": 1,
        "strategy_pnl_pts": 24.0,
        "real_pnl_pts": 22.5,
        "edge_erosion_pts": 1.5,
        "edge_erosion_dollars": 0.15,
        "avg_entry_slippage_pts": 0.4,
        "worst_entry_slippage_pts": 0.7,
        "avg_total_latency_ms": 240,
        "p95_total_latency_ms": 350,
        "worst_total_latency_ms": 600,
    }
    embed = _build_discord_embed(
        strategy_label="velocity",
        account_label="Audit demo",
        day_str="2026-06-08",
        summary=summary,
    )
    assert "embeds" in embed
    field_values = " ".join(f["value"] for f in embed["embeds"][0]["fields"])
    assert "24.00" in field_values  # strategy_pnl_pts
    assert "22.50" in field_values  # real_pnl_pts
    assert "1.50" in field_values   # edge erosion
    assert "240" in field_values    # latency


def test_json_payload_wraps_summary_with_event_metadata():
    from app.integrations.partner_daily_summary import _build_json_payload

    body = _build_json_payload(
        grant_id=7,
        account_id=42,
        tradelocker_account_id="2163244",
        day_str="2026-06-08",
        summary={"trades_closed": 1},
    )
    assert body["event"] == "daily_summary"
    assert body["grant_id"] == 7
    assert body["tradelocker_account_id"] == "2163244"
    assert body["summary"] == {"trades_closed": 1}


# ----------------------------------------------------------------------- #
# publish_for_grant
# ----------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_publish_for_grant_posts_json_with_hmac(
    db_session, owner, partner, trading_account
):
    from app.integrations.partner_daily_summary import publish_for_grant

    grant = _make_grant(
        db_session,
        account_id=trading_account.id,
        grantee_id=partner.id,
        owner_id=owner.id,
        url="https://partner.example.com/daily",
    )
    _make_closed_record(
        db_session, owner_user_id=owner.id, tl_account_id="2163244"
    )

    captured: dict = {}

    async def fake_post(self, url, content, headers):
        captured["url"] = url
        captured["body"] = content
        captured["headers"] = dict(headers)
        return httpx.Response(200, request=httpx.Request("POST", url))

    with patch("httpx.AsyncClient.post", new=fake_post):
        ok = await publish_for_grant(
            grant,
            day=datetime.utcnow() - timedelta(days=1),
            db=db_session,
        )

    assert ok is True
    assert captured["url"] == "https://partner.example.com/daily"
    assert captured["headers"]["X-Event"] == "daily_summary"
    assert captured["headers"]["X-Signature"].startswith("sha256=")
    body = json.loads(captured["body"])
    assert body["event"] == "daily_summary"
    assert body["summary"]["trades_closed"] == 1


@pytest.mark.asyncio
async def test_publish_for_grant_uses_discord_embed_for_discord_url(
    db_session, owner, partner, trading_account
):
    from app.integrations.partner_daily_summary import publish_for_grant

    grant = _make_grant(
        db_session,
        account_id=trading_account.id,
        grantee_id=partner.id,
        owner_id=owner.id,
        url="https://discord.com/api/webhooks/123/abc",
    )
    _make_closed_record(
        db_session, owner_user_id=owner.id, tl_account_id="2163244"
    )

    captured: dict = {}

    async def fake_post(self, url, content, headers):
        captured["body"] = content
        captured["headers"] = dict(headers)
        return httpx.Response(200, request=httpx.Request("POST", url))

    with patch("httpx.AsyncClient.post", new=fake_post):
        ok = await publish_for_grant(
            grant,
            day=datetime.utcnow() - timedelta(days=1),
            db=db_session,
        )

    assert ok is True
    body = json.loads(captured["body"])
    assert "embeds" in body
    # Discord rejects custom X-* headers
    assert "X-Signature" not in captured["headers"]


@pytest.mark.asyncio
async def test_publish_for_grant_returns_false_on_5xx(
    db_session, owner, partner, trading_account
):
    from app.integrations.partner_daily_summary import publish_for_grant
    import app.integrations.partner_webhook as pw

    grant = _make_grant(
        db_session,
        account_id=trading_account.id,
        grantee_id=partner.id,
        owner_id=owner.id,
        url="https://down.example.com/daily",
    )

    async def fake_post(self, url, content, headers):
        return httpx.Response(500, request=httpx.Request("POST", url))

    # Shrink retry delays so the test isn't slow.
    with patch.object(pw, "_RETRY_DELAYS_SEC", (0, 0, 0)), patch(
        "httpx.AsyncClient.post", new=fake_post
    ):
        ok = await publish_for_grant(
            grant,
            day=datetime.utcnow() - timedelta(days=1),
            db=db_session,
        )
    assert ok is False


@pytest.mark.asyncio
async def test_publish_for_grant_no_url_returns_false(
    db_session, owner, partner, trading_account
):
    from app.integrations.partner_daily_summary import publish_for_grant

    grant = AccountAccessGrant(
        account_id=trading_account.id,
        grantee_user_id=partner.id,
        role=AccountAccessRole.viewer,
        granted_by_user_id=owner.id,
        partner_webhook_url=None,
        partner_webhook_secret_encrypted=None,
    )
    db_session.add(grant)
    db_session.commit()

    ok = await publish_for_grant(
        grant, day=datetime.utcnow() - timedelta(days=1), db=db_session
    )
    assert ok is False


# ----------------------------------------------------------------------- #
# maybe_publish — the cron tick worker
# ----------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_maybe_publish_fires_for_each_active_grant(
    db_session, owner, partner, trading_account
):
    from app.integrations.partner_daily_summary import maybe_publish

    _make_grant(
        db_session,
        account_id=trading_account.id,
        grantee_id=partner.id,
        owner_id=owner.id,
        url="https://partner.example.com/daily",
    )

    async def fake_post(self, url, content, headers):
        return httpx.Response(200, request=httpx.Request("POST", url))

    with patch("httpx.AsyncClient.post", new=fake_post):
        posted = await maybe_publish()
    assert posted == 1


@pytest.mark.asyncio
async def test_maybe_publish_dedups_within_day(
    db_session, owner, partner, trading_account
):
    """Two ticks during the same UTC day → only the first sends."""
    from app.integrations.partner_daily_summary import maybe_publish

    _make_grant(
        db_session,
        account_id=trading_account.id,
        grantee_id=partner.id,
        owner_id=owner.id,
        url="https://partner.example.com/daily",
    )

    post_count = 0

    async def fake_post(self, url, content, headers):
        nonlocal post_count
        post_count += 1
        return httpx.Response(200, request=httpx.Request("POST", url))

    with patch("httpx.AsyncClient.post", new=fake_post):
        await maybe_publish()
        await maybe_publish()
    assert post_count == 1


@pytest.mark.asyncio
async def test_maybe_publish_skips_revoked_and_expired_grants(
    db_session, owner, partner, trading_account
):
    from app.integrations.partner_daily_summary import maybe_publish

    _make_grant(
        db_session,
        account_id=trading_account.id,
        grantee_id=partner.id,
        owner_id=owner.id,
        url="https://revoked.example.com/daily",
    ).revoked_at = datetime.utcnow()
    db_session.commit()

    # And an expired one for good measure
    stale = AccountAccessGrant(
        account_id=trading_account.id,
        grantee_user_id=partner.id,
        role=AccountAccessRole.viewer,
        granted_by_user_id=owner.id,
        partner_webhook_url="https://expired.example.com/daily",
        partner_webhook_secret_encrypted=encrypt("s"),
        expires_at=datetime.utcnow() - timedelta(hours=1),
    )
    db_session.add(stale)
    db_session.commit()

    post_count = 0

    async def fake_post(self, url, content, headers):
        nonlocal post_count
        post_count += 1
        return httpx.Response(200, request=httpx.Request("POST", url))

    with patch("httpx.AsyncClient.post", new=fake_post):
        posted = await maybe_publish()
    assert posted == 0
    assert post_count == 0
