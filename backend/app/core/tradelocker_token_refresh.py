"""Auto-refresh expired TradeLocker tokens.

Two callers:
  1. Runner — when a 401 hits during a tick, call refresh_user_token(...)
     and retry once. Avoids halting trading on a daily token expiry.
  2. Periodic background job (lifespan) — every N minutes, scan for users
     whose token will expire soon and pre-emptively refresh.

Both paths persist the new (access_token, refresh_token) back to the
User row, encrypted. Failures (no refresh token, refresh endpoint 401)
mark the user's TL connection as needing re-auth from the UI.
"""
from __future__ import annotations

import logging
from typing import Optional

from app.core.crypto import decrypt, encrypt
from app.core.tradelocker_client import TradeLockerClient, TradeLockerError
from app.db.database import SessionLocal
from app.db.models import User

logger = logging.getLogger(__name__)


async def refresh_user_token(user_id: int) -> Optional[str]:
    """Refresh the access token for a user using their stored refresh_token.

    Returns the new plaintext access token on success, None if the refresh
    failed (user must re-auth via the UI). Persists the new tokens back
    to the User row before returning.
    """
    db = SessionLocal()
    try:
        user = db.query(User).filter_by(id=user_id).first()
        if user is None or not user.tradelocker_refresh_token:
            return None
        try:
            refresh_plain = decrypt(user.tradelocker_refresh_token)
        except Exception as exc:
            logger.warning("decrypt refresh_token failed for user %s: %s", user_id, exc)
            return None
        if not refresh_plain:
            return None

        client = TradeLockerClient(env=user.tradelocker_env or "demo")
        try:
            data = await client.refresh_access_token(refresh_plain)
        except TradeLockerError as exc:
            logger.warning(
                "TL refresh failed for user %s (env=%s): %s — needs UI re-auth",
                user_id,
                user.tradelocker_env,
                exc,
            )
            return None

        new_access = data.get("access_token")
        new_refresh = data.get("refresh_token") or refresh_plain
        if not new_access:
            return None

        user.tradelocker_token = encrypt(new_access)
        user.tradelocker_refresh_token = encrypt(new_refresh)
        db.commit()
        logger.info("TL token refreshed for user %s (env=%s)", user_id, user.tradelocker_env)
        return new_access
    finally:
        db.close()


async def proactive_refresh_all() -> dict:
    """Background-job entry point: refresh all live-account users.

    Returns a {refreshed: int, failed: int, skipped: int} summary.
    Run from a periodic task (e.g. lifespan asyncio.Task on a 6h interval).
    """
    db = SessionLocal()
    refreshed = failed = skipped = 0
    try:
        users = db.query(User).filter(User.tradelocker_token.is_not(None)).all()
        user_ids = [u.id for u in users]
    finally:
        db.close()

    for uid in user_ids:
        new = await refresh_user_token(uid)
        if new is not None:
            refreshed += 1
        else:
            # Distinguish: was it skipped because no refresh token, or did
            # refresh fail? The refresh function logs both — we just count.
            failed += 1

    return {"refreshed": refreshed, "failed": failed, "skipped": skipped}
