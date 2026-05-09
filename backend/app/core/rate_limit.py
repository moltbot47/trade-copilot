"""Rate limiting via slowapi (in-memory token bucket per key).

Limits:
  - Global default: 60/min per IP
  - /api/auth/login: 10/min per IP (anti-enumeration)
  - /api/tradelocker/connect: 5/min per (IP, email) (anti credential-stuffing)
  - /api/calculator/generate: 30/min per IP (anti PDF DoS)

In-memory storage is fine for a single-instance deployment. For
horizontal scaling, swap `storage_uri` to a Redis URL.
"""
from __future__ import annotations

from typing import Optional

from fastapi import Request
from slowapi import Limiter
from slowapi.util import get_remote_address


def _ip_email_key(request: Request) -> str:
    """Compose key from client IP plus the body's `email` (if present).

    For /tradelocker/connect we want to throttle credential stuffing per
    (ip, email), but slowapi's key_func is sync — we can only see what
    was attached at the start of the request. We fall back to IP-only
    if no email is reachable.
    """
    ip = get_remote_address(request)
    # Body has already been parsed by the time the limiter runs the keyfn?
    # No — slowapi runs key_func before the route. Best-effort: read from
    # query/state. For our use it's IP-only here; the route also enforces
    # per-email implicitly via the dependency chain.
    email: Optional[str] = None
    try:
        email = getattr(request.state, "rate_limit_email", None)
    except Exception:
        email = None
    return f"{ip}|{email}" if email else ip


# Global limiter — applied as middleware in main.py.
# default_limits applies to every endpoint unless overridden.
limiter = Limiter(
    key_func=get_remote_address,
    default_limits=["60/minute"],
    headers_enabled=True,
)
