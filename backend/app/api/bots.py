"""Public bot catalog endpoints."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.db.database import get_db
from app.db.models import Bot
from app.schemas import BotOut

router = APIRouter(prefix="/bots", tags=["bots"])


@router.get("", response_model=list[BotOut])
def list_bots(db: Session = Depends(get_db)) -> list[BotOut]:
    bots = db.query(Bot).filter(Bot.is_active.is_(True)).order_by(Bot.id.asc()).all()
    return [BotOut.model_validate(b) for b in bots]


@router.get("/{bot_id}", response_model=BotOut)
def get_bot(bot_id: int, db: Session = Depends(get_db)) -> BotOut:
    bot = db.get(Bot, bot_id)
    if bot is None:
        raise HTTPException(status_code=404, detail="bot not found")
    return BotOut.model_validate(bot)
