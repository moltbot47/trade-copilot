"""TradeLocker session auth helpers — shared refresh-on-401 logic.

Three callers (reconciliation, position_monitor, signal_router) previously each
implemented their own 401-handling and most just gave up on it. data_feed.py
implemented refresh-and-retry correctly. This module consolidates that pattern
so every caller gets the same behavior:

  - Load decrypted token + account info from the User row
  - Run the broker call
  - On 401, call refresh_user_token() and retry ONCE with the new token
  - If the retry still 401s, raise — the user must re-authenticate via UI

Also exposes validate_user_tl_session() used by /strategy/start to fail loudly
instead of silently spawning a runner that dies on the first broker call.
"""
from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable, Optional, TypeVar

from app.core.crypto import decrypt
from app.core.tradelocker_client import TradeLockerClient, TradeLockerError
from app.core.tradelocker_token_refresh import refresh_user_token
from app.db import database as _db
from app.db.database import SessionLocal  # re-exported for tests that patch it here
from app.db.models import User

logger = logging.getLogger(__name__)

T = TypeVar("T")


def _is_auth_error(exc: BaseException) -> bool:
    msg = str(exc).lower()
    return "401" in msg or "unauthorized" in msg


def load_session(user_id: int, db=None) -> Optional[dict]:
    """Return the user's current TL session info decrypted, or None.

    Shape: {token, account_id, acc_num, env}.

    If `db` is provided, uses it (caller-owned, not closed). Otherwise
    opens its own SessionLocal session. Allowing the caller to pass their
    own session matters when tests bind to an in-memory engine that isn't
    the module's frozen SessionLocal — and avoids opening a redundant
    connection when the caller already has one.
    """
    own_session = db is None
    if own_session:
        db = SessionLocal()
    try:
        user = db.query(User).filter(User.id == user_id).first()
        if user is None or not user.tradelocker_token or not user.tradelocker_account_id:
            return None
        token = decrypt(user.tradelocker_token)
        if not token:
            return None
        return {
            "token": token,
            "account_id": user.tradelocker_account_id,
            "acc_num": user.tradelocker_acc_num or "1",
            "env": user.tradelocker_env or "demo",
        }
    finally:
        if own_session:
            db.close()


async def call_with_refresh(
    user_id: int,
    op: Callable[[dict], Awaitable[T]],
    db=None,
) -> T:
    """Run op(session) with auto-refresh-on-401 retry.

    `op` is a coroutine that takes a session dict ({token, account_id, acc_num,
    env}) and returns any value. Typical usage:

        async def do_call(s):
            client = TradeLockerClient(env=s["env"])
            return await client.get_positions(
                s["account_id"], s["token"], s["acc_num"]
            )
        positions = await call_with_refresh(user_id, do_call)

    On 401 from the first call, attempts refresh_user_token() and retries
    op() once with the new session. If refresh fails OR retry still 401s,
    re-raises the underlying TradeLockerError so the caller can decide
    what to do (log, alert, mark needs-reauth, etc.).

    Pass `db` if you already have a Session; otherwise we open our own.

    Raises ValueError if the user has no session at all.
    """
    session = load_session(user_id, db=db)
    if session is None:
        raise ValueError(f"user {user_id} has no TradeLocker session")

    try:
        return await op(session)
    except TradeLockerError as exc:
        if not _is_auth_error(exc):
            raise
        # Try once to refresh.
        logger.info("call_with_refresh: 401 for user=%s, attempting refresh", user_id)
        new_token = await refresh_user_token(user_id)
        if not new_token:
            logger.warning(
                "call_with_refresh: refresh FAILED for user=%s (needs UI re-auth)",
                user_id,
            )
            raise
        # Reload session — refresh_user_token persisted new tokens to DB.
        # If the caller passed `db`, expire its cache so the freshly-rotated
        # token from another connection is picked up.
        if db is not None:
            try:
                db.expire_all()
            except Exception:
                pass
        session = load_session(user_id, db=db)
        if session is None:
            raise
        try:
            result = await op(session)
            logger.info("call_with_refresh: retry succeeded for user=%s", user_id)
            return result
        except TradeLockerError as exc2:
            if _is_auth_error(exc2):
                logger.warning(
                    "call_with_refresh: still 401 after refresh for user=%s "
                    "(refresh token may also be expired)",
                    user_id,
                )
            raise


async def validate_user_tl_session(user_id: int, db=None) -> tuple[bool, str]:
    """Cheap probe to verify the user's TL session works. Refreshes if needed.

    Used by /strategy/start to block startup on dead sessions instead of
    silently spawning a runner that dies on the first broker call.

    Pass `db` if you already have a Session (e.g. in a request handler) —
    we need to read the User row from the same engine, otherwise we'd open
    a fresh SessionLocal that may point at a different DB in tests.

    Returns (is_valid, reason):
      (True,  "ok")               session works (with or without a refresh)
      (False, "no_session")       no token stored for this user
      (False, "needs_reauth")     401 and refresh also failed
      (False, "network_error")    connectivity issue, retry later
    """
    session = load_session(user_id, db=db)
    if session is None:
        return False, "no_session"

    async def _probe(s: dict) -> Any:
        client = TradeLockerClient(env=s["env"])
        # list_all_accounts is the cheapest authenticated probe — just hits
        # /auth/jwt/all-accounts, no per-account data needed.
        return await client.list_all_accounts(s["token"])

    try:
        await call_with_refresh(user_id, _probe, db=db)
        return True, "ok"
    except TradeLockerError as exc:
        if _is_auth_error(exc):
            return False, "needs_reauth"
        logger.warning("validate_user_tl_session network/other error user=%s: %s", user_id, exc)
        return False, "network_error"
    except Exception as exc:  # noqa: BLE001
        logger.warning("validate_user_tl_session unexpected error user=%s: %s", user_id, exc)
        return False, "network_error"
