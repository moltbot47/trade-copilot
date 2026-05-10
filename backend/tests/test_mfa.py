"""Tests for TOTP MFA setup, verification, and login integration."""
from __future__ import annotations

import time

import pyotp
import pytest

from app.auth.mfa import (
    decrypt_secret,
    encrypt_secret,
    generate_secret,
    provisioning_uri,
    verify_code,
)


# ---------------------------------------------------------------------------
# Unit tests on the mfa module
# ---------------------------------------------------------------------------

def test_generate_secret_is_base32_and_unique():
    s1 = generate_secret()
    s2 = generate_secret()
    assert s1 != s2
    # base32 chars
    assert all(c in "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567" for c in s1)
    assert len(s1) >= 16  # pyotp default is 32 chars


def test_provisioning_uri_contains_email_and_issuer():
    secret = generate_secret()
    uri = provisioning_uri(secret, "user@example.com")
    assert uri.startswith("otpauth://totp/")
    assert "user%40example.com" in uri or "user@example.com" in uri
    assert "Trade%20Copilot" in uri or "Trade Copilot" in uri
    assert f"secret={secret}" in uri


def test_verify_code_accepts_current_code():
    secret = generate_secret()
    current = pyotp.TOTP(secret).now()
    assert verify_code(secret, current) is True


def test_verify_code_rejects_wrong_code():
    secret = generate_secret()
    assert verify_code(secret, "000000") is False


def test_verify_code_rejects_non_digits():
    secret = generate_secret()
    assert verify_code(secret, "abcdef") is False
    assert verify_code(secret, "12345") is False  # too short
    assert verify_code(secret, "1234567") is False  # too long
    assert verify_code(secret, "") is False


def test_verify_code_strips_whitespace():
    secret = generate_secret()
    current = pyotp.TOTP(secret).now()
    assert verify_code(secret, f" {current} ") is True
    assert verify_code(secret, f"{current[:3]} {current[3:]}") is True


def test_verify_code_handles_none_secret():
    assert verify_code(None, "123456") is False
    assert verify_code("", "123456") is False


def test_verify_code_within_window():
    """RFC 6238 allows ±1 step (30s) tolerance."""
    secret = generate_secret()
    # Code from the previous 30s window should still validate
    prev_window = pyotp.TOTP(secret).at(time.time() - 30)
    assert verify_code(secret, prev_window) is True
    # Code from 2 windows ago should NOT validate
    too_old = pyotp.TOTP(secret).at(time.time() - 90)
    assert verify_code(secret, too_old) is False


def test_encrypt_decrypt_roundtrip():
    secret = generate_secret()
    encrypted = encrypt_secret(secret)
    assert encrypted != secret  # actually encrypted
    decrypted = decrypt_secret(encrypted)
    assert decrypted == secret


def test_decrypt_secret_handles_none():
    assert decrypt_secret(None) is None
    assert decrypt_secret("") is None


def test_decrypt_secret_handles_garbage():
    assert decrypt_secret("not_valid_fernet_token") is None


# ---------------------------------------------------------------------------
# HTTP integration tests
# ---------------------------------------------------------------------------

@pytest.fixture
def authed_client(client, db_session):
    """A TestClient with a valid session cookie for an existing user."""
    from app.api.users import get_or_create_user
    from app.core.jwt import issue_session_token
    from app.config import get_settings

    user = get_or_create_user(db_session, "mfa-test@example.com")
    db_session.commit()
    token, _exp = issue_session_token(user.email)
    settings = get_settings()
    client.cookies.set(settings.SESSION_COOKIE_NAME, token)
    return client, user


def test_mfa_status_returns_disabled_by_default(authed_client):
    client, _user = authed_client
    r = client.get("/api/auth/mfa/status")
    assert r.status_code == 200
    assert r.json() == {"enabled": False}


def test_mfa_setup_returns_secret_and_uri(authed_client, db_session):
    client, user = authed_client
    r = client.post("/api/auth/mfa/setup")
    assert r.status_code == 200
    body = r.json()
    assert "secret" in body
    assert body["otpauth_uri"].startswith("otpauth://totp/")
    assert body["enabled"] is False

    db_session.refresh(user)
    assert user.mfa_secret is not None      # encrypted secret persisted
    assert user.mfa_enabled is False        # not yet active


def test_mfa_setup_rejects_when_already_enabled(authed_client, db_session):
    client, user = authed_client
    user.mfa_secret = encrypt_secret(generate_secret())
    user.mfa_enabled = True
    db_session.commit()

    r = client.post("/api/auth/mfa/setup")
    assert r.status_code == 409
    assert "already_enabled" in r.json()["detail"]


def test_mfa_verify_activates_with_valid_code(authed_client, db_session):
    client, user = authed_client
    secret = generate_secret()
    user.mfa_secret = encrypt_secret(secret)
    user.mfa_enabled = False
    db_session.commit()

    code = pyotp.TOTP(secret).now()
    r = client.post("/api/auth/mfa/verify", json={"code": code})
    assert r.status_code == 200
    assert r.json() == {"enabled": True}

    db_session.refresh(user)
    assert user.mfa_enabled is True


def test_mfa_verify_rejects_bad_code(authed_client, db_session):
    client, user = authed_client
    user.mfa_secret = encrypt_secret(generate_secret())
    user.mfa_enabled = False
    db_session.commit()

    r = client.post("/api/auth/mfa/verify", json={"code": "000000"})
    assert r.status_code == 401


def test_mfa_disable_clears_secret(authed_client, db_session):
    client, user = authed_client
    secret = generate_secret()
    user.mfa_secret = encrypt_secret(secret)
    user.mfa_enabled = True
    db_session.commit()

    code = pyotp.TOTP(secret).now()
    r = client.post("/api/auth/mfa/disable", json={"code": code})
    assert r.status_code == 200
    assert r.json() == {"enabled": False}

    db_session.refresh(user)
    assert user.mfa_enabled is False
    assert user.mfa_secret is None


def test_mfa_disable_requires_valid_code(authed_client, db_session):
    client, user = authed_client
    user.mfa_secret = encrypt_secret(generate_secret())
    user.mfa_enabled = True
    db_session.commit()

    r = client.post("/api/auth/mfa/disable", json={"code": "000000"})
    assert r.status_code == 401
    db_session.refresh(user)
    assert user.mfa_enabled is True  # still enabled — attacker can't bypass


# ---------------------------------------------------------------------------
# Login integration — MFA enforced on /api/auth/login
# ---------------------------------------------------------------------------

def test_login_works_when_mfa_disabled(client, db_session):
    """Existing email-only flow continues to work for users without MFA."""
    r = client.post("/api/auth/login", json={"email": "no-mfa@example.com"})
    assert r.status_code == 200
    body = r.json()
    assert body["email"] == "no-mfa@example.com"


def test_login_requires_mfa_code_when_enabled(client, db_session):
    from app.api.users import get_or_create_user

    user = get_or_create_user(db_session, "mfa-on@example.com")
    user.mfa_secret = encrypt_secret(generate_secret())
    user.mfa_enabled = True
    db_session.commit()

    r = client.post("/api/auth/login", json={"email": user.email})
    assert r.status_code == 401
    assert r.json()["detail"] == "mfa_required"


def test_login_succeeds_with_valid_mfa_code(client, db_session):
    from app.api.users import get_or_create_user

    user = get_or_create_user(db_session, "mfa-on2@example.com")
    secret = generate_secret()
    user.mfa_secret = encrypt_secret(secret)
    user.mfa_enabled = True
    db_session.commit()

    code = pyotp.TOTP(secret).now()
    r = client.post(
        "/api/auth/login",
        json={"email": user.email, "mfa_code": code},
    )
    assert r.status_code == 200
    assert r.json()["email"] == user.email


def test_login_rejects_invalid_mfa_code(client, db_session):
    from app.api.users import get_or_create_user

    user = get_or_create_user(db_session, "mfa-on3@example.com")
    user.mfa_secret = encrypt_secret(generate_secret())
    user.mfa_enabled = True
    db_session.commit()

    r = client.post(
        "/api/auth/login",
        json={"email": user.email, "mfa_code": "000000"},
    )
    assert r.status_code == 401
    assert r.json()["detail"] == "mfa_invalid_code"
