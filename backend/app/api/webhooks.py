"""TradingView webhook receiver.

Authentication
--------------
Two modes are supported during the migration window:

1. **Hardened mode (preferred)** — TradingView (or a relay in front of it)
   sends three headers along with the JSON body:

     X-Bot-Slug:          <bot.slug>
     X-Webhook-Timestamp: <unix-seconds>
     X-Webhook-Signature: <hex HMAC-SHA256 of f"{ts}.{raw_body}" with bot.webhook_secret>

   The receiver verifies the signature in constant time and rejects any
   request whose timestamp is outside ``MAX_AGE_SEC`` (default 300s).
   Any verification failure produces a generic 401 — never leak which
   check failed (avoids slug-enumeration / replay-tuning oracles).

2. **Legacy mode (deprecated)** — the request body carries
   ``"bot_secret": "<bot.slug>"`` and no signature headers. We still accept
   these requests so existing TradingView alerts keep firing, but we emit a
   structured deprecation warning per request. This path is scheduled for
   removal — see CHANGELOG.

R-12 mitigation: see docs/RISK_MATRIX.md.
"""
from __future__ import annotations

import json
import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.core.discord_notifier import post_signal_to_discord
from app.core.signal_router import fan_out
from app.core.webhook_signing import verify_signature
from app.db.database import get_db
from app.db.models import Bot, Signal
from app.schemas import TradingViewSignal, WebhookResponse

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/webhooks", tags=["webhooks"])

# Mirrors `verify_signature(max_age_sec=...)` default. Kept here so the
# /api/bots/{slug}/webhook endpoint can advertise the same value.
MAX_AGE_SEC = 300

# Generic, intentionally vague — never tells the caller which check failed.
_GENERIC_AUTH_ERROR = "invalid webhook"


def _reject() -> HTTPException:
    return HTTPException(status_code=401, detail=_GENERIC_AUTH_ERROR)


@router.post("/tradingview", response_model=WebhookResponse)
async def tradingview_webhook(
    request: Request, db: Session = Depends(get_db)
) -> WebhookResponse:
    # Read the raw body ONCE — must sign exactly the bytes we received.
    raw_body = await request.body()

    bot_slug_header = request.headers.get("X-Bot-Slug")
    ts_header = request.headers.get("X-Webhook-Timestamp")
    sig_header = request.headers.get("X-Webhook-Signature")

    has_signed_headers = any(h is not None for h in (ts_header, sig_header))

    # Parse the JSON body for downstream use. Done first so 422-style
    # malformed bodies are rejected before we expose any auth-path nuances.
    try:
        body_json = json.loads(raw_body or b"{}")
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="invalid json body")

    bot: Bot | None = None
    legacy_path = False

    if bot_slug_header is not None or has_signed_headers:
        # ---- Hardened mode: identify by slug header, verify signature ----
        if not bot_slug_header:
            # Headers leaked partial intent (sig present, slug missing).
            raise _reject()
        if not ts_header or not sig_header:
            raise _reject()
        try:
            ts_int = int(ts_header)
        except (TypeError, ValueError):
            raise _reject()

        bot = db.query(Bot).filter(Bot.slug == bot_slug_header).first()
        if bot is None or not bot.is_active:
            # Don't leak whether the slug exists — same response as a
            # signature mismatch.
            raise _reject()
        if not verify_signature(
            bot.webhook_secret, raw_body, ts_int, sig_header,
            max_age_sec=MAX_AGE_SEC,
        ):
            raise _reject()
    else:
        # ---- Legacy mode: bot_secret in body (deprecated) ----
        # Run Pydantic body validation FIRST so malformed payloads continue
        # to surface as 422 (the historical contract); we don't want the
        # auth-missing branch to hide a 422 from clients still in this path.
        try:
            TradingViewSignal.model_validate(body_json)
        except ValidationError as exc:
            raise RequestValidationError(exc.errors()) from exc

        body_secret = body_json.get("bot_secret")
        if not body_secret or not isinstance(body_secret, str):
            raise HTTPException(status_code=400, detail="missing authentication")
        bot = db.query(Bot).filter(Bot.slug == body_secret).first()
        if bot is None or not bot.is_active:
            # Legacy path: keep the original 404 contract for existing tests
            # / clients that branch on it. The hardened path is the only one
            # that returns the slug-opaque 401.
            raise HTTPException(status_code=404, detail="bot not found or inactive")
        legacy_path = True
        logger.warning(
            "webhook.deprecated_body_secret bot_slug=%s — switch to "
            "X-Webhook-Signature headers; body bot_secret is scheduled for removal",
            bot.slug,
        )

    # At this point we have a valid, active bot. Validate payload shape.
    try:
        payload = TradingViewSignal.model_validate(body_json)
    except ValidationError as exc:
        # Surface as 422 — same status code FastAPI emits for request-body
        # validation failures, so existing clients/tests stay consistent.
        raise RequestValidationError(exc.errors()) from exc

    signal = Signal(
        bot_id=bot.id,
        instrument=payload.instrument,
        side=payload.side,
        entry_price=payload.entry_price,
        stop_loss=payload.stop_loss,
        take_profit=payload.take_profit,
        base_lot_size=payload.base_lot_size,
        raw_payload=json.dumps(payload.model_dump()),
    )
    db.add(signal)
    db.commit()
    db.refresh(signal)

    # Discord broadcast (best-effort)
    try:
        await post_signal_to_discord(
            bot_name=bot.name,
            instrument=payload.instrument,
            side=payload.side,
            entry=payload.entry_price,
            sl=payload.stop_loss,
            tp=payload.take_profit,
            base_lot=payload.base_lot_size,
        )
    except Exception as exc:  # never fail the webhook on Discord errors
        logger.warning("discord notify failed: %s", exc)

    # Fan out to all subscribers
    try:
        executions = await fan_out(signal, db)
    except Exception as exc:
        logger.exception("fan_out failed: %s", exc)
        executions = []

    # Status is "received" for both paths so existing dashboards/tests keep
    # working; the deprecation signal lives in the WARN log line above.
    _ = legacy_path  # marker for grep; no behavioural change
    return WebhookResponse(
        status="received",
        signal_id=signal.id,
        subscribers_notified=len(executions),
    )
