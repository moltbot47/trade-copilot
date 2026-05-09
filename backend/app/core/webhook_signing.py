"""HMAC-SHA256 signing/verification for inbound webhook payloads.

Why
---
The legacy webhook used `bot_secret = bot.slug` in the JSON body for
authentication. Slugs are public catalog data, so anyone could spoof
TradingView and inject arbitrary fills (R-12 in the risk matrix).

This module replaces that scheme with an HMAC-SHA256 signature over a
timestamp-prefixed payload, signed with a per-bot secret. The receiver
verifies:
  1. The timestamp is within `max_age_sec` of "now" (replay protection).
  2. The signature matches `HMAC(secret, f"{ts}.{body}")` using a
     constant-time compare (`hmac.compare_digest`).

The format is intentionally similar to Stripe's webhook signing scheme so
existing relay/proxy code patterns translate cleanly.
"""
from __future__ import annotations

import hashlib
import hmac
import time


def sign_payload(secret: str, body: bytes, timestamp: int) -> str:
    """Compute the hex HMAC-SHA256 over ``f"{timestamp}.{body}"``.

    Args:
        secret: Per-bot webhook secret (raw string, not base64).
        body: Raw request body bytes — sign exactly what will be sent on the wire.
        timestamp: Unix epoch seconds. Caller controls clock so tests stay deterministic.

    Returns:
        Lowercase hex digest. Length is 64 chars (32 bytes * 2).
    """
    if not isinstance(body, (bytes, bytearray)):
        raise TypeError("body must be bytes")
    if not isinstance(secret, str) or not secret:
        raise ValueError("secret must be a non-empty string")
    msg = f"{int(timestamp)}.".encode("utf-8") + bytes(body)
    return hmac.new(secret.encode("utf-8"), msg, hashlib.sha256).hexdigest()


def verify_signature(
    secret: str,
    body: bytes,
    timestamp: int,
    signature: str,
    *,
    max_age_sec: int = 300,
    now: int | None = None,
) -> bool:
    """Constant-time verify a webhook signature.

    Returns False (never raises) on:
      - missing/empty inputs
      - timestamp older than ``max_age_sec``
      - timestamp more than ``max_age_sec`` in the future (clock skew bound)
      - signature mismatch

    The caller MUST treat any False as a 401 and respond with a generic
    message — never leak which check failed.
    """
    if not signature or not secret:
        return False
    try:
        ts_int = int(timestamp)
    except (TypeError, ValueError):
        return False
    current = int(now if now is not None else time.time())
    if abs(current - ts_int) > max_age_sec:
        return False
    expected = sign_payload(secret, body, ts_int)
    # Constant-time compare to prevent timing side-channels.
    return hmac.compare_digest(expected, signature)
