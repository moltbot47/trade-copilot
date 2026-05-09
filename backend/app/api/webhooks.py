"""TradingView webhook receiver."""
from __future__ import annotations

import json
import logging

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.core.discord_notifier import post_signal_to_discord
from app.core.signal_router import fan_out
from app.db.database import get_db
from app.db.models import Bot, Signal
from app.schemas import TradingViewSignal, WebhookResponse

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/webhooks", tags=["webhooks"])


@router.post("/tradingview", response_model=WebhookResponse)
async def tradingview_webhook(
    payload: TradingViewSignal, db: Session = Depends(get_db)
) -> WebhookResponse:
    bot = db.query(Bot).filter(Bot.slug == payload.bot_secret).first()
    if bot is None or not bot.is_active:
        raise HTTPException(status_code=404, detail="bot not found or inactive")

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

    return WebhookResponse(
        status="received",
        signal_id=signal.id,
        subscribers_notified=len(executions),
    )
