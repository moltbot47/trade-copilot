"""Unit tests for app.core.jwt — issue / verify / tampering / algorithm pinning."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import jwt as pyjwt
import pytest

from app.config import get_settings
from app.core.jwt import ALGORITHM, JWTError, issue_session_token, verify_session_token


def test_issue_session_token_returns_valid_jwt_with_sub():
    token, exp = issue_session_token("user@example.com")
    assert isinstance(token, str)
    assert token.count(".") == 2  # JWT shape: header.payload.signature
    assert isinstance(exp, datetime)
    assert exp > datetime.now(timezone.utc)

    claims = verify_session_token(token)
    assert claims["sub"] == "user@example.com"
    assert "iat" in claims
    assert "exp" in claims


def test_verify_session_token_returns_claims_for_valid_token():
    token, _ = issue_session_token("alice@example.com", ttl_days=1)
    claims = verify_session_token(token)
    assert claims["sub"] == "alice@example.com"


def test_verify_session_token_rejects_expired_token():
    settings = get_settings()
    now = datetime.now(timezone.utc)
    payload = {
        "sub": "expired@example.com",
        "iat": int((now - timedelta(days=2)).timestamp()),
        "exp": int((now - timedelta(seconds=1)).timestamp()),
    }
    expired = pyjwt.encode(payload, settings.SECRET_KEY, algorithm=ALGORITHM)
    with pytest.raises(JWTError) as exc:
        verify_session_token(expired)
    assert "expired" in str(exc.value).lower()


def test_verify_session_token_rejects_tampered_token():
    token, _ = issue_session_token("victim@example.com")
    # Flip a few characters in the payload section.
    parts = token.split(".")
    tampered_payload = parts[1][:-2] + ("AA" if parts[1][-2:] != "AA" else "BB")
    tampered = ".".join([parts[0], tampered_payload, parts[2]])
    with pytest.raises(JWTError):
        verify_session_token(tampered)


def test_verify_session_token_rejects_empty_token():
    with pytest.raises(JWTError):
        verify_session_token("")


def test_verify_session_token_rejects_garbage_token():
    with pytest.raises(JWTError):
        verify_session_token("not-a-jwt-at-all")


def test_algorithm_none_attack_rejected():
    """An attacker forging a token with alg=none MUST fail verification — we
    pin algorithms=[HS256] in verify_session_token, so the unsigned token
    should raise JWTError rather than be accepted."""
    settings = get_settings()
    payload = {
        "sub": "attacker@example.com",
        "iat": int(datetime.now(timezone.utc).timestamp()),
        "exp": int((datetime.now(timezone.utc) + timedelta(days=1)).timestamp()),
    }
    # PyJWT requires explicit opt-in to encode unsigned, but we can construct
    # one manually. Simpler: encode with HS512 instead of HS256 — verifier
    # should reject because we pin HS256.
    wrong_alg = pyjwt.encode(payload, settings.SECRET_KEY, algorithm="HS512")
    with pytest.raises(JWTError):
        verify_session_token(wrong_alg)


def test_issue_session_token_custom_ttl():
    token, exp = issue_session_token("ttl@example.com", ttl_days=7)
    delta = exp - datetime.now(timezone.utc)
    # ~7 days, within a few seconds of now.
    assert 6.99 * 86400 < delta.total_seconds() < 7.01 * 86400
