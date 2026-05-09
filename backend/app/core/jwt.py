"""JWT issue/verify helpers for session cookies.

HS256-signed tokens carrying {sub: email, exp: unix_ts, iat: unix_ts}.
Signed with settings.SECRET_KEY — config refuses production boot with
the placeholder default, so tokens are non-spoofable in deployed envs.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import jwt as pyjwt

from app.config import get_settings

ALGORITHM = "HS256"


class JWTError(Exception):
    """Raised when a token is missing, malformed, expired, or has a bad signature."""


def issue_session_token(email: str, ttl_days: int | None = None) -> tuple[str, datetime]:
    """Sign and return (token, expires_at_utc) for the given email."""
    settings = get_settings()
    ttl = ttl_days if ttl_days is not None else settings.SESSION_TTL_DAYS
    now = datetime.now(timezone.utc)
    exp = now + timedelta(days=ttl)
    payload = {
        "sub": email,
        "iat": int(now.timestamp()),
        "exp": int(exp.timestamp()),
    }
    token = pyjwt.encode(payload, settings.SECRET_KEY, algorithm=ALGORITHM)
    return token, exp


def verify_session_token(token: str) -> dict[str, Any]:
    """Decode token and return its claims.

    Raises JWTError on any failure (bad signature, expired, malformed).
    """
    settings = get_settings()
    try:
        return pyjwt.decode(token, settings.SECRET_KEY, algorithms=[ALGORITHM])
    except pyjwt.ExpiredSignatureError as exc:
        raise JWTError("token expired") from exc
    except pyjwt.InvalidTokenError as exc:
        raise JWTError(f"invalid token: {exc}") from exc
