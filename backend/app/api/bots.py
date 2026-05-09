"""Public bot catalog + authenticated-owner webhook secret management."""
from __future__ import annotations

import secrets

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app.api.users import get_current_user
from app.db.database import get_db
from app.db.models import Bot, User
from app.schemas import BotOut, BotWebhookSecretOut

router = APIRouter(prefix="/bots", tags=["bots"])


# Generated on row creation by Bot.webhook_secret default (matches that factory).
def _new_secret() -> str:
    return secrets.token_urlsafe(32)


def _build_webhook_url(request: Request) -> str:
    """Reconstruct the public webhook URL from the request — works behind proxies
    that set the standard X-Forwarded-* headers, falls back to ``request.url``."""
    forwarded_proto = request.headers.get("x-forwarded-proto")
    forwarded_host = request.headers.get("x-forwarded-host")
    scheme = forwarded_proto or request.url.scheme
    host = forwarded_host or request.url.netloc
    return f"{scheme}://{host}/api/webhooks/tradingview"


@router.get("", response_model=list[BotOut])
def list_bots(db: Session = Depends(get_db)) -> list[BotOut]:
    bots = db.query(Bot).filter(Bot.is_active.is_(True)).order_by(Bot.id.asc()).all()
    # BotOut intentionally OMITS webhook_secret. Never leak the secret here.
    return [BotOut.model_validate(b) for b in bots]


@router.get("/{bot_id}", response_model=BotOut)
def get_bot(bot_id: int, db: Session = Depends(get_db)) -> BotOut:
    bot = db.get(Bot, bot_id)
    if bot is None:
        raise HTTPException(status_code=404, detail="bot not found")
    return BotOut.model_validate(bot)


@router.get("/{slug}/webhook", response_model=BotWebhookSecretOut)
def get_bot_webhook_secret(
    slug: str,
    request: Request,
    db: Session = Depends(get_db),
    _user: User = Depends(get_current_user),
) -> BotWebhookSecretOut:
    """Return the live HMAC secret for a bot. Requires authentication.

    Any authenticated user can fetch any bot's secret in this MVP — the
    bots are a shared catalog (not multi-tenant) and each subscriber needs
    the secret to wire up TradingView. If/when bots become user-scoped,
    add an ownership check here.
    """
    bot = db.query(Bot).filter(Bot.slug == slug).first()
    if bot is None:
        raise HTTPException(status_code=404, detail="bot not found")
    return BotWebhookSecretOut(
        slug=bot.slug,
        webhook_url=_build_webhook_url(request),
        secret=bot.webhook_secret,
    )


@router.post("/{slug}/webhook/rotate", response_model=BotWebhookSecretOut)
def rotate_bot_webhook_secret(
    slug: str,
    request: Request,
    db: Session = Depends(get_db),
    _user: User = Depends(get_current_user),
) -> BotWebhookSecretOut:
    """Generate a new webhook secret and invalidate the old one.

    The new secret is returned exactly once in the response body — the old
    secret is overwritten in place and is unrecoverable.
    """
    bot = db.query(Bot).filter(Bot.slug == slug).first()
    if bot is None:
        raise HTTPException(status_code=404, detail="bot not found")
    bot.webhook_secret = _new_secret()
    db.commit()
    db.refresh(bot)
    return BotWebhookSecretOut(
        slug=bot.slug,
        webhook_url=_build_webhook_url(request),
        secret=bot.webhook_secret,
    )
