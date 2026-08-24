"""Public bot catalog + authenticated-owner webhook secret management."""
from __future__ import annotations

import re
import secrets

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app.api.users import get_current_user
from app.db.database import get_db
from app.db.models import Bot, User
from app.schemas import BotOut, BotWebhookSecretOut
from app.strategies.performance_tracker import PerformanceTracker

router = APIRouter(prefix="/bots", tags=["bots"])

# Rolling window (closed trades) used for the marketplace's live stats. Wide
# enough to be meaningful, small enough to reflect recent behavior.
_LIVE_STATS_WINDOW = 50

# Defense-in-depth: never leak an email address through a bot's public
# description, even if one was entered during partner onboarding. The catalog
# is public, so we scrub here regardless of what's stored in the row.
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")


def _redact_emails(text: str) -> str:
    return _EMAIL_RE.sub("[hidden]", text or "")


def _bot_out(db: Session, bot: Bot) -> BotOut:
    """Serialize a bot for the public catalog, attaching live performance
    computed from its real closed trades and scrubbing any leaked email."""
    out = BotOut.model_validate(bot)
    out.description = _redact_emails(out.description)

    stats = PerformanceTracker(db, bot.id).compute_rolling_stats(window=_LIVE_STATS_WINDOW)
    n = int(stats.get("total_trades", 0))
    if n > 0:
        # compute_rolling_stats returns win_rate as a 0..1 fraction; the
        # marketplace/backtest convention is a percentage (62.0 == 62%).
        out.live_win_rate = round(float(stats["win_rate"]) * 100.0, 1)
        out.live_profit_factor = round(float(stats["profit_factor"]), 2)
        out.live_total_trades = n
        out.stats_source = "live"
    elif bot.backtest_win_rate > 0 or bot.backtest_profit_factor > 0:
        out.stats_source = "backtest"
    else:
        out.stats_source = "none"
    return out


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
    return [_bot_out(db, b) for b in bots]


@router.get("/{bot_id}", response_model=BotOut)
def get_bot(bot_id: int, db: Session = Depends(get_db)) -> BotOut:
    bot = db.get(Bot, bot_id)
    if bot is None:
        raise HTTPException(status_code=404, detail="bot not found")
    return _bot_out(db, bot)


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
