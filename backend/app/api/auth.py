"""Authentication endpoints: login, logout, and session inspection.

Login issues a signed JWT in an HttpOnly+SameSite=Lax cookie named
`tc_session`. The MVP "no password" feel is preserved (any email
auto-creates a row), but the session is now signed and non-spoofable.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, EmailStr
from sqlalchemy.orm import Session

from app.api.users import get_current_user, get_or_create_user
from app.config import get_settings
from app.core.jwt import issue_session_token
from app.core.rate_limit import limiter
from app.db.database import get_db
from app.db.models import User

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/auth", tags=["auth"])


class LoginRequest(BaseModel):
    email: EmailStr


class LoginResponse(BaseModel):
    email: EmailStr
    exp: int  # unix seconds


class MeResponse(BaseModel):
    email: EmailStr
    tradelocker_account_id: str | None = None
    tradelocker_env: str = "demo"


def _set_session_cookie(response: Response, token: str, max_age_seconds: int) -> None:
    settings = get_settings()
    response.set_cookie(
        key=settings.SESSION_COOKIE_NAME,
        value=token,
        max_age=max_age_seconds,
        httponly=True,
        samesite="lax",
        secure=settings.cookie_secure,
        path="/",
    )


def _clear_session_cookie(response: Response) -> None:
    settings = get_settings()
    response.delete_cookie(
        key=settings.SESSION_COOKIE_NAME,
        path="/",
        samesite="lax",
        secure=settings.cookie_secure,
    )


@router.post("/login", response_model=LoginResponse)
@limiter.limit("10/minute")
def login(
    request: Request,
    payload: LoginRequest,
    response: Response,
    db: Session = Depends(get_db),
) -> LoginResponse:
    """Issue a session cookie for the given email.

    First-time emails auto-create a User row (MVP: no password barrier).
    Rate-limited to 10/min per IP to deter enumeration.
    """
    settings = get_settings()
    user = get_or_create_user(db, str(payload.email))
    token, expires_at = issue_session_token(user.email)
    max_age = settings.SESSION_TTL_DAYS * 24 * 60 * 60
    _set_session_cookie(response, token, max_age)
    logger.info("login: %s (exp=%s)", user.email, expires_at.isoformat())

    # Boot the TradeLocker relay if the user has a stored token.
    # Best-effort — failures here must not block login.
    if user.tradelocker_token and user.tradelocker_account_id:
        try:
            from app.ws.relay_manager import relay_manager

            relay_manager.start_for_user(user.id)
        except Exception as exc:  # pragma: no cover — defensive
            logger.debug("relay_manager.start_for_user skipped: %s", exc)

    return LoginResponse(email=user.email, exp=int(expires_at.timestamp()))


@router.post("/logout")
def logout(
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
) -> dict[str, str]:
    """Clear the session cookie. Always 200 — idempotent."""
    _clear_session_cookie(response)

    # Stop any running TL relay for this user. We resolve the user from
    # the cookie/header (best-effort) so we don't require auth on logout.
    try:
        from app.api.users import _extract_token

        token = _extract_token(request)
        if token:
            from app.core.jwt import JWTError, verify_session_token

            try:
                claims = verify_session_token(token)
                email = claims.get("sub")
                if email:
                    user = db.query(User).filter(User.email == email).first()
                    if user is not None:
                        from app.ws.relay_manager import relay_manager
                        import asyncio

                        try:
                            asyncio.get_running_loop().create_task(
                                relay_manager.stop_for_user(user.id)
                            )
                        except RuntimeError:
                            # No running loop (sync context) — schedule via run
                            try:
                                asyncio.run(relay_manager.stop_for_user(user.id))
                            except Exception:
                                pass
            except JWTError:
                pass
    except Exception as exc:  # pragma: no cover — defensive
        logger.debug("logout relay-stop skipped: %s", exc)

    return {"status": "logged_out"}


@router.get("/me", response_model=MeResponse)
def me(user: User = Depends(get_current_user)) -> MeResponse:
    """Return the current user's identity. 401 if no valid session."""
    return MeResponse(
        email=user.email,
        tradelocker_account_id=user.tradelocker_account_id,
        tradelocker_env=user.tradelocker_env or "demo",
    )
