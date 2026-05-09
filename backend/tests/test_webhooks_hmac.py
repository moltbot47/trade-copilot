"""HMAC webhook signing tests.

Covers:
  - happy path with valid signature → 200
  - tampered body → 401 with generic message
  - bad signature → 401
  - timestamp older than 5 min → 401
  - timestamp far in the future → 401
  - missing X-Bot-Slug header → 401
  - non-existent slug → 401 (no leak)
  - legacy body-secret path still works + emits deprecation warning
  - constant-time compare is in use (source-level assertion)
  - rotate endpoint changes the secret and invalidates the old one
"""
from __future__ import annotations

import inspect as _inspect
import json
import logging
import time
from unittest.mock import AsyncMock, patch

import pytest

from app.core import webhook_signing
from app.core.webhook_signing import sign_payload


WEBHOOK_PATH = "/api/webhooks/tradingview"


def _no_broker():
    """Patch fan_out + Discord so tests don't touch the network."""
    return (
        patch("app.api.webhooks.fan_out", new=AsyncMock(return_value=[])),
        patch("app.api.webhooks.post_signal_to_discord", new=AsyncMock(return_value=None)),
    )


def _post_signed(client, slug, secret, body_dict, *, ts=None, signature=None, extra_headers=None):
    """Build a signed webhook request from primitives. Each arg can be overridden."""
    body_bytes = json.dumps(body_dict).encode("utf-8")
    ts_value = int(ts if ts is not None else time.time())
    sig_value = signature if signature is not None else sign_payload(secret, body_bytes, ts_value)
    headers = {
        "Content-Type": "application/json",
        "X-Bot-Slug": slug,
        "X-Webhook-Timestamp": str(ts_value),
        "X-Webhook-Signature": sig_value,
    }
    if extra_headers:
        headers.update(extra_headers)
    return client.post(WEBHOOK_PATH, content=body_bytes, headers=headers)


# --------------------------- helpers / unit tests ---------------------------

def test_sign_and_verify_roundtrip():
    body = b'{"hello":"world"}'
    ts = 1_700_000_000
    sig = sign_payload("abc", body, ts)
    assert webhook_signing.verify_signature("abc", body, ts, sig, now=ts)


def test_verify_rejects_old_timestamp():
    body = b"{}"
    ts = 1_000_000
    sig = sign_payload("s", body, ts)
    assert not webhook_signing.verify_signature("s", body, ts, sig, max_age_sec=60, now=ts + 3600)


def test_verify_rejects_future_timestamp():
    body = b"{}"
    ts = 5_000_000
    sig = sign_payload("s", body, ts)
    assert not webhook_signing.verify_signature("s", body, ts, sig, max_age_sec=60, now=ts - 3600)


def test_verify_rejects_tampered_body():
    body = b'{"a":1}'
    ts = 1_700_000_000
    sig = sign_payload("s", body, ts)
    assert not webhook_signing.verify_signature("s", b'{"a":2}', ts, sig, now=ts)


def test_verify_uses_constant_time_compare():
    """Source-level guarantee that we use hmac.compare_digest, not ==."""
    src = _inspect.getsource(webhook_signing.verify_signature)
    assert "hmac.compare_digest" in src, (
        "verify_signature MUST use hmac.compare_digest to avoid timing attacks"
    )


# --------------------------- end-to-end (HTTP) ---------------------------


def test_webhook_valid_signature_returns_200(client, seed_bots):
    p1, p2 = _no_broker()
    body = {
        "instrument": "EURUSD",
        "side": "buy",
        "entry_price": 1.10,
        "stop_loss": 1.09,
        "take_profit": 1.12,
        "base_lot_size": 0.1,
    }
    with p1, p2:
        res = _post_signed(client, "orb-breakout", "test-secret-orb-breakout", body)
    assert res.status_code == 200, res.text
    j = res.json()
    assert j["status"] == "received"
    assert j["signal_id"] > 0


def test_webhook_invalid_signature_returns_401_generic(client, seed_bots):
    p1, p2 = _no_broker()
    body = {"instrument": "EURUSD", "side": "buy"}
    with p1, p2:
        res = _post_signed(
            client, "orb-breakout", "test-secret-orb-breakout", body,
            signature="0" * 64,
        )
    assert res.status_code == 401
    assert res.json()["detail"] == "invalid webhook"


def test_webhook_old_timestamp_returns_401(client, seed_bots):
    p1, p2 = _no_broker()
    body = {"instrument": "EURUSD", "side": "buy"}
    too_old = int(time.time()) - 3600
    with p1, p2:
        res = _post_signed(client, "orb-breakout", "test-secret-orb-breakout", body, ts=too_old)
    assert res.status_code == 401


def test_webhook_future_timestamp_returns_401(client, seed_bots):
    p1, p2 = _no_broker()
    body = {"instrument": "EURUSD", "side": "buy"}
    too_new = int(time.time()) + 3600
    with p1, p2:
        res = _post_signed(client, "orb-breakout", "test-secret-orb-breakout", body, ts=too_new)
    assert res.status_code == 401


def test_webhook_missing_bot_slug_header_returns_401(client, seed_bots):
    """If signature/timestamp headers exist but slug is absent, reject."""
    p1, p2 = _no_broker()
    body_bytes = json.dumps({"instrument": "EURUSD", "side": "buy"}).encode("utf-8")
    ts = int(time.time())
    sig = sign_payload("test-secret-orb-breakout", body_bytes, ts)
    headers = {
        "Content-Type": "application/json",
        # X-Bot-Slug intentionally omitted.
        "X-Webhook-Timestamp": str(ts),
        "X-Webhook-Signature": sig,
    }
    with p1, p2:
        res = client.post(WEBHOOK_PATH, content=body_bytes, headers=headers)
    assert res.status_code == 401
    assert res.json()["detail"] == "invalid webhook"


def test_webhook_unknown_slug_returns_401_no_leak(client, seed_bots):
    """Non-existent slug must look identical to a signature mismatch."""
    p1, p2 = _no_broker()
    body = {"instrument": "EURUSD", "side": "buy"}
    with p1, p2:
        res = _post_signed(client, "no-such-bot", "any-secret-here", body)
    assert res.status_code == 401
    assert res.json()["detail"] == "invalid webhook"


def test_webhook_tampered_body_returns_401(client, seed_bots):
    """Sign one body, send another → mismatch."""
    p1, p2 = _no_broker()
    signed_body = json.dumps({"instrument": "EURUSD", "side": "buy"}).encode("utf-8")
    ts = int(time.time())
    sig = sign_payload("test-secret-orb-breakout", signed_body, ts)
    tampered_body = json.dumps({"instrument": "EURUSD", "side": "sell"}).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "X-Bot-Slug": "orb-breakout",
        "X-Webhook-Timestamp": str(ts),
        "X-Webhook-Signature": sig,
    }
    with p1, p2:
        res = client.post(WEBHOOK_PATH, content=tampered_body, headers=headers)
    assert res.status_code == 401


def test_webhook_legacy_body_secret_still_works_with_warning(client, seed_bots, caplog):
    """Backwards-compat: body-only bot_secret returns 200 + logs WARN."""
    p1, p2 = _no_broker()
    body = {
        "bot_secret": "orb-breakout",
        "instrument": "EURUSD",
        "side": "buy",
    }
    with p1, p2, caplog.at_level(logging.WARNING, logger="app.api.webhooks"):
        res = client.post(WEBHOOK_PATH, json=body)
    assert res.status_code == 200, res.text
    # The log message advertises the migration path.
    assert any(
        "deprecated_body_secret" in rec.message and "orb-breakout" in rec.message
        for rec in caplog.records
    ), [r.message for r in caplog.records]


def test_webhook_inactive_bot_via_signed_path_returns_401(client, db_session, seed_bots):
    """Hardened path treats inactive bot the same as a sig mismatch."""
    from app.db.models import Bot

    bot = db_session.query(Bot).filter(Bot.slug == "orb-breakout").first()
    bot.is_active = False
    db_session.commit()

    p1, p2 = _no_broker()
    body = {"instrument": "EURUSD", "side": "buy"}
    with p1, p2:
        res = _post_signed(client, "orb-breakout", "test-secret-orb-breakout", body)
    assert res.status_code == 401


def test_webhook_invalid_timestamp_format_returns_401(client, seed_bots):
    p1, p2 = _no_broker()
    body_bytes = json.dumps({"instrument": "EURUSD", "side": "buy"}).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "X-Bot-Slug": "orb-breakout",
        "X-Webhook-Timestamp": "not-a-number",
        "X-Webhook-Signature": "0" * 64,
    }
    with p1, p2:
        res = client.post(WEBHOOK_PATH, content=body_bytes, headers=headers)
    assert res.status_code == 401


# --------------------------- /api/bots/{slug}/webhook ---------------------------


def test_get_webhook_secret_requires_auth(client, seed_bots):
    res = client.get("/api/bots/orb-breakout/webhook")
    assert res.status_code == 401


def test_get_webhook_secret_returns_secret_to_authenticated_user(
    client, seed_bots, auth_headers
):
    res = client.get("/api/bots/orb-breakout/webhook", headers=auth_headers)
    assert res.status_code == 200, res.text
    j = res.json()
    assert j["slug"] == "orb-breakout"
    assert j["secret"] == "test-secret-orb-breakout"
    assert "/api/webhooks/tradingview" in j["webhook_url"]
    assert j["max_age_seconds"] == 300


def test_list_bots_does_not_leak_webhook_secret(client, seed_bots):
    res = client.get("/api/bots")
    assert res.status_code == 200
    for bot in res.json():
        assert "webhook_secret" not in bot
        assert "secret" not in bot


def test_rotate_webhook_secret_changes_value_and_invalidates_old(
    client, seed_bots, auth_headers, db_session
):
    p1, p2 = _no_broker()
    # Step 1: confirm baseline
    res = client.get("/api/bots/orb-breakout/webhook", headers=auth_headers)
    old_secret = res.json()["secret"]
    assert old_secret == "test-secret-orb-breakout"

    # Step 2: rotate
    res = client.post("/api/bots/orb-breakout/webhook/rotate", headers=auth_headers)
    assert res.status_code == 200, res.text
    new_secret = res.json()["secret"]
    assert new_secret != old_secret
    assert len(new_secret) >= 30  # token_urlsafe(32) → ~43 chars

    # Step 3: signing with the OLD secret should now fail (401).
    body = {"instrument": "EURUSD", "side": "buy"}
    with p1, p2:
        res = _post_signed(client, "orb-breakout", old_secret, body)
    assert res.status_code == 401

    # Step 4: signing with the NEW secret should succeed (200).
    p1, p2 = _no_broker()
    with p1, p2:
        res = _post_signed(client, "orb-breakout", new_secret, body)
    assert res.status_code == 200


def test_rotate_requires_auth(client, seed_bots):
    res = client.post("/api/bots/orb-breakout/webhook/rotate")
    assert res.status_code == 401


def test_get_secret_unknown_slug_returns_404(client, seed_bots, auth_headers):
    res = client.get("/api/bots/no-such-bot/webhook", headers=auth_headers)
    assert res.status_code == 404
