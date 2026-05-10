"""Tests for the audit log + account lockout features."""
from __future__ import annotations

import json
from datetime import datetime, timedelta

import pyotp
import pytest

from app.auth.mfa import encrypt_secret, generate_secret
from app.core.audit import record_audit
from app.db.models import AuditLog, User


# ---------- record_audit unit tests ----------

def test_record_audit_creates_row(db_session):
    user = User(email="aud@example.com", hashed_password="x")
    db_session.add(user)
    db_session.flush()

    record_audit(db_session, user=user, action="login_success", details={"src": "web"})
    db_session.commit()

    rows = db_session.query(AuditLog).filter(AuditLog.user_id == user.id).all()
    assert len(rows) == 1
    assert rows[0].action == "login_success"
    assert json.loads(rows[0].details) == {"src": "web"}


def test_record_audit_anonymous_user(db_session):
    """Anonymous events (e.g. login_failed for unknown email) should record
    with user_id=None and no actor_email."""
    record_audit(db_session, action="webhook_signature_invalid", details={"bot_slug": "x"})
    db_session.commit()
    rows = db_session.query(AuditLog).filter(AuditLog.action == "webhook_signature_invalid").all()
    assert len(rows) == 1
    assert rows[0].user_id is None
    assert rows[0].actor_email is None


def test_record_audit_handles_unserializable_details(db_session):
    """default=str in json.dumps should let datetimes through without crashing."""
    record_audit(db_session, action="risk_setting_changed",
                 details={"changed_at": datetime.utcnow(), "old": 1, "new": 3})
    db_session.commit()  # must not raise
    rows = db_session.query(AuditLog).all()
    assert len(rows) == 1


# ---------- Login flow: lockout + audit ----------

def test_login_records_audit_on_success(client, db_session):
    r = client.post("/api/auth/login", json={"email": "alice@example.com"})
    assert r.status_code == 200
    user = db_session.query(User).filter(User.email == "alice@example.com").first()
    assert user is not None
    rows = db_session.query(AuditLog).filter(AuditLog.user_id == user.id).all()
    assert any(row.action == "login_success" for row in rows)


def test_failed_mfa_increments_counter(client, db_session):
    from app.api.users import get_or_create_user
    user = get_or_create_user(db_session, "lock1@example.com")
    user.mfa_secret = encrypt_secret(generate_secret())
    user.mfa_enabled = True
    db_session.commit()

    for _ in range(3):
        r = client.post("/api/auth/login",
                        json={"email": user.email, "mfa_code": "000000"})
        assert r.status_code == 401

    db_session.refresh(user)
    assert user.failed_login_count == 3
    assert user.locked_until is None  # not locked yet


def test_account_locks_after_5_failed_mfa_attempts(client, db_session):
    from app.api.users import get_or_create_user
    user = get_or_create_user(db_session, "lock2@example.com")
    user.mfa_secret = encrypt_secret(generate_secret())
    user.mfa_enabled = True
    db_session.commit()

    for _ in range(5):
        client.post("/api/auth/login",
                    json={"email": user.email, "mfa_code": "000000"})

    db_session.refresh(user)
    assert user.failed_login_count >= 5
    assert user.locked_until is not None
    assert user.locked_until > datetime.utcnow()

    # 6th attempt should now return 429 account_locked
    r = client.post("/api/auth/login",
                    json={"email": user.email, "mfa_code": "000000"})
    assert r.status_code == 429
    assert r.json()["detail"] == "account_locked"
    assert "Retry-After" in r.headers


def test_successful_login_resets_lockout_counter(client, db_session):
    from app.api.users import get_or_create_user

    user = get_or_create_user(db_session, "lock3@example.com")
    secret = generate_secret()
    user.mfa_secret = encrypt_secret(secret)
    user.mfa_enabled = True
    user.failed_login_count = 3
    db_session.commit()

    code = pyotp.TOTP(secret).now()
    r = client.post("/api/auth/login",
                    json={"email": user.email, "mfa_code": code})
    assert r.status_code == 200

    db_session.refresh(user)
    assert user.failed_login_count == 0
    assert user.locked_until is None
    rows = db_session.query(AuditLog).filter(
        AuditLog.user_id == user.id,
        AuditLog.action == "account_unlocked",
    ).all()
    assert len(rows) >= 1


def test_locked_account_expires_naturally(client, db_session):
    """A lockout in the past should not block a fresh attempt."""
    from app.api.users import get_or_create_user

    user = get_or_create_user(db_session, "lock4@example.com")
    user.locked_until = datetime.utcnow() - timedelta(minutes=30)  # expired
    user.failed_login_count = 5
    db_session.commit()

    r = client.post("/api/auth/login", json={"email": user.email})
    assert r.status_code == 200  # No MFA, no lock-block


def test_locked_account_returns_429_during_window(client, db_session):
    from app.api.users import get_or_create_user

    user = get_or_create_user(db_session, "lock5@example.com")
    user.locked_until = datetime.utcnow() + timedelta(minutes=10)
    db_session.commit()

    r = client.post("/api/auth/login", json={"email": user.email})
    assert r.status_code == 429


# ---------- Audit-log read endpoint ----------

def test_audit_log_endpoint_returns_user_own_entries_only(client, db_session):
    """User A's audit log must not leak User B's actions."""
    from app.api.users import get_or_create_user
    from app.core.jwt import issue_session_token
    from app.config import get_settings

    user_a = get_or_create_user(db_session, "audA@example.com")
    user_b = get_or_create_user(db_session, "audB@example.com")
    db_session.flush()

    record_audit(db_session, user=user_a, action="login_success")
    record_audit(db_session, user=user_b, action="login_success")
    db_session.commit()

    token, _ = issue_session_token(user_a.email)
    client.cookies.set(get_settings().SESSION_COOKIE_NAME, token)

    r = client.get("/api/auth/audit-log")
    assert r.status_code == 200
    items = r.json()
    assert all(i["action"] == "login_success" for i in items)
    # user B's entries are NOT included
    assert len(items) == 1


def test_audit_log_endpoint_requires_auth(client):
    r = client.get("/api/auth/audit-log")
    assert r.status_code == 401
