"""User registration and identity helpers.

`get_current_user` is the single source of identity for protected routes.
It reads a signed JWT from either:
  - the `tc_session` cookie (browser sessions), or
  - an `Authorization: Bearer <token>` header (API clients).

Anything else → 401. The previous X-User-Email "trust the header" path
is removed entirely.
"""
from __future__ import annotations

import re
import secrets

import bcrypt
from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.config import get_settings
from app.core.jwt import JWTError, verify_session_token
from app.db.database import get_db
from app.db.models import User
from app.schemas import UserCreate, UserOut

router = APIRouter(prefix="/users", tags=["users"])


def _hash_password(plain: str) -> str:
    return bcrypt.hashpw(plain.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def _extract_token(request: Request) -> str | None:
    """Pull session token from cookie first, then Authorization header."""
    settings = get_settings()
    cookie_val = request.cookies.get(settings.SESSION_COOKIE_NAME)
    if cookie_val:
        return cookie_val
    auth = request.headers.get("authorization")
    if auth and auth.lower().startswith("bearer "):
        return auth.split(" ", 1)[1].strip() or None
    return None


def get_current_user(
    request: Request,
    db: Session = Depends(get_db),
) -> User:
    """Return the User row for the authenticated session, or raise 401.

    Auto-creates a User row on first login (handled by /auth/login) — by
    the time a request reaches this dependency, the user must already
    exist; we only validate the token here.
    """
    token = _extract_token(request)
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="not authenticated",
            headers={"WWW-Authenticate": "Bearer"},
        )
    try:
        claims = verify_session_token(token)
    except JWTError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"invalid session: {exc}",
            headers={"WWW-Authenticate": "Bearer"},
        )
    email = claims.get("sub")
    if not email or not isinstance(email, str):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="malformed token")
    user = db.query(User).filter(User.email == email).first()
    if user is None:
        # Token valid but user vanished — treat as unauthenticated.
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="user not found")
    return user


def get_or_create_user(db: Session, email: str) -> User:
    """Idempotent fetch-or-create. Used by /auth/login.

    Preserves the MVP's frictionless "just-give-an-email" feel — but the
    session that comes out is now a signed JWT, not a spoofable header.
    """
    user = db.query(User).filter(User.email == email).first()
    if user is None:
        user = User(
            email=email,
            hashed_password=_hash_password(secrets.token_urlsafe(24)),
        )
        db.add(user)
        db.commit()
        db.refresh(user)
    return user


@router.post("", response_model=UserOut, status_code=status.HTTP_201_CREATED)
def create_user(payload: UserCreate, db: Session = Depends(get_db)) -> UserOut:
    existing = db.query(User).filter(User.email == payload.email).first()
    if existing:
        raise HTTPException(status_code=409, detail="email already registered")
    user = User(email=payload.email, hashed_password=_hash_password(payload.password))
    db.add(user)
    db.commit()
    db.refresh(user)
    return UserOut.model_validate(user)


@router.get("/me", response_model=UserOut)
def read_me(user: User = Depends(get_current_user)) -> UserOut:
    return UserOut.model_validate(user)


# ----- Per-user notification webhook (Discord) -----

_DISCORD_WEBHOOK_PATTERN = re.compile(
    r"^https://discord\.com/api/webhooks/\d+/[A-Za-z0-9_\-]+$"
)


class DiscordWebhookRequest(BaseModel):
    url: str | None  # None to clear


class DiscordWebhookStatus(BaseModel):
    has_webhook: bool
    masked: str | None = None  # last 6 chars only for display


def _mask_webhook(url: str | None) -> str | None:
    if not url:
        return None
    if len(url) <= 12:
        return "***"
    return f"...{url[-6:]}"


@router.get("/me/discord-webhook", response_model=DiscordWebhookStatus)
def get_discord_webhook(user: User = Depends(get_current_user)) -> DiscordWebhookStatus:
    return DiscordWebhookStatus(
        has_webhook=bool(user.discord_webhook_url),
        masked=_mask_webhook(user.discord_webhook_url),
    )


@router.put("/me/discord-webhook", response_model=DiscordWebhookStatus)
def set_discord_webhook(
    payload: DiscordWebhookRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> DiscordWebhookStatus:
    """Save the user's personal Discord webhook URL. Pass null to clear."""
    if payload.url is None or payload.url.strip() == "":
        user.discord_webhook_url = None
    else:
        url = payload.url.strip()
        if not _DISCORD_WEBHOOK_PATTERN.match(url):
            raise HTTPException(
                status_code=400,
                detail="invalid_discord_webhook_url — expected https://discord.com/api/webhooks/<id>/<token>",
            )
        user.discord_webhook_url = url
    db.commit()
    from app.core.audit import record_audit
    record_audit(db, user=user, action="risk_setting_changed",
                 details={"field": "discord_webhook_url", "set": bool(user.discord_webhook_url)})
    db.commit()
    return DiscordWebhookStatus(
        has_webhook=bool(user.discord_webhook_url),
        masked=_mask_webhook(user.discord_webhook_url),
    )


class PanicRequest(BaseModel):
    paused: bool


class PanicStatus(BaseModel):
    bot_paused: bool


@router.get("/me/panic", response_model=PanicStatus)
def get_panic(user: User = Depends(get_current_user)) -> PanicStatus:
    return PanicStatus(bot_paused=bool(user.bot_paused))


@router.post("/me/panic", response_model=PanicStatus)
def set_panic(
    payload: PanicRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> PanicStatus:
    """Toggle the global bot_paused flag.

    When True, the runner skips every entry decision for this user. Any
    open positions continue to be managed (trailing stops still trail);
    but no new entries will fire until the user clears the panic state.
    """
    prev = bool(user.bot_paused)
    user.bot_paused = bool(payload.paused)
    db.commit()
    from app.core.audit import record_audit
    record_audit(
        db, user=user,
        action="bot_paused" if user.bot_paused else "bot_unpaused",
        details={"prev": prev, "new": user.bot_paused},
    )
    db.commit()
    return PanicStatus(bot_paused=user.bot_paused)


@router.post("/me/discord-webhook/test")
async def test_discord_webhook(user: User = Depends(get_current_user)) -> dict:
    """Send a test message to the user's configured webhook to verify it."""
    if not user.discord_webhook_url:
        raise HTTPException(status_code=400, detail="no_webhook_configured")
    import httpx
    try:
        async with httpx.AsyncClient(timeout=5.0) as c:
            r = await c.post(
                user.discord_webhook_url,
                json={
                    "embeds": [{
                        "title": "🔌 Trade Copilot webhook test",
                        "description": f"Your bot will post signals here. user={user.email}",
                        "color": 0x00C853,
                    }]
                },
            )
            if r.status_code >= 400:
                raise HTTPException(
                    status_code=400,
                    detail=f"webhook_rejected status={r.status_code}",
                )
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"webhook_unreachable: {exc}")
    return {"status": "sent"}
