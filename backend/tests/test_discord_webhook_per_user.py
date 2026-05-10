"""Tests for per-user Discord webhook URLs."""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from app.db.models import User


def _auth_user(client, db_session, email: str = "wh@example.com") -> User:
    from app.api.users import get_or_create_user
    from app.core.jwt import issue_session_token
    from app.config import get_settings

    user = get_or_create_user(db_session, email)
    db_session.commit()
    token, _ = issue_session_token(user.email)
    client.cookies.set(get_settings().SESSION_COOKIE_NAME, token)
    return user


def test_get_discord_webhook_returns_disabled_by_default(client, db_session):
    _auth_user(client, db_session)
    r = client.get("/api/users/me/discord-webhook")
    assert r.status_code == 200
    assert r.json() == {"has_webhook": False, "masked": None}


def test_set_discord_webhook_accepts_valid_url(client, db_session):
    user = _auth_user(client, db_session)
    url = "https://discord.com/api/webhooks/1503081025746505900/ObLJYIVblV1EWsYBeaF6IG0lk4kC_YrY_xL"
    r = client.put("/api/users/me/discord-webhook", json={"url": url})
    assert r.status_code == 200
    body = r.json()
    assert body["has_webhook"] is True
    assert body["masked"].startswith("...")
    db_session.refresh(user)
    assert user.discord_webhook_url == url


def test_set_discord_webhook_rejects_invalid_url(client, db_session):
    _auth_user(client, db_session)
    r = client.put(
        "/api/users/me/discord-webhook",
        json={"url": "https://evil.example.com/api/webhooks/123/abc"},
    )
    assert r.status_code == 400
    assert "invalid_discord_webhook_url" in r.json()["detail"]


def test_set_discord_webhook_clears_with_null(client, db_session):
    user = _auth_user(client, db_session)
    user.discord_webhook_url = "https://discord.com/api/webhooks/123/abc"
    db_session.commit()
    r = client.put("/api/users/me/discord-webhook", json={"url": None})
    assert r.status_code == 200
    assert r.json()["has_webhook"] is False
    db_session.refresh(user)
    assert user.discord_webhook_url is None


def test_test_webhook_returns_400_when_not_configured(client, db_session):
    _auth_user(client, db_session)
    r = client.post("/api/users/me/discord-webhook/test")
    assert r.status_code == 400
    assert "no_webhook" in r.json()["detail"]


def test_test_webhook_posts_to_user_url(client, db_session):
    user = _auth_user(client, db_session)
    user.discord_webhook_url = "https://discord.com/api/webhooks/1234/abcdef"
    db_session.commit()

    posted = {}

    class FakeResp:
        status_code = 204
        text = ""

    class FakeClient:
        def __init__(self, *a, **kw):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            pass
        async def post(self, url, json):
            posted["url"] = url
            posted["payload"] = json
            return FakeResp()

    with patch("httpx.AsyncClient", FakeClient):
        r = client.post("/api/users/me/discord-webhook/test")
    assert r.status_code == 200
    assert r.json() == {"status": "sent"}
    assert posted["url"] == user.discord_webhook_url


def test_webhook_url_helper_prefers_user_over_env(db_session, monkeypatch):
    """_webhook_url(user_id) returns user's URL even if env has a global one."""
    from app.api.users import get_or_create_user
    from app.integrations.discord_signals import _webhook_url

    user = get_or_create_user(db_session, "webhook-helper@example.com")
    user.discord_webhook_url = "https://discord.com/api/webhooks/USER/AAA"
    db_session.commit()

    # Force a global env webhook
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", "https://discord.com/api/webhooks/GLOBAL/BBB")

    # The helper does `from app.db.database import SessionLocal` at call
    # time. Patch the source-of-truth module so the function picks up the
    # test session.
    from sqlalchemy.orm import sessionmaker
    import app.db.database as db_mod

    TestSession = sessionmaker(bind=db_session.get_bind(), autoflush=False, autocommit=False)
    with patch.object(db_mod, "SessionLocal", TestSession):
        # Without user_id → returns global
        assert _webhook_url() == "https://discord.com/api/webhooks/GLOBAL/BBB"
        # With user_id → returns user's
        assert _webhook_url(user.id) == "https://discord.com/api/webhooks/USER/AAA"


def test_audit_log_entry_recorded_on_webhook_change(client, db_session):
    user = _auth_user(client, db_session)
    url = "https://discord.com/api/webhooks/1503081025746505900/ObLJYIVblV1EWsYBeaF6IG0lk4kC_YrY_xL"
    client.put("/api/users/me/discord-webhook", json={"url": url})

    from app.db.models import AuditLog
    rows = (
        db_session.query(AuditLog)
        .filter(AuditLog.user_id == user.id, AuditLog.action == "risk_setting_changed")
        .all()
    )
    assert any("discord_webhook_url" in r.details for r in rows)
