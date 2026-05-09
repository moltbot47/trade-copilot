"""Dashboard PnL + open position endpoints."""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.api.users import get_current_user
from app.core.crypto import decrypt
from app.core.tradelocker_client import TradeLockerClient, TradeLockerError
from app.db.database import get_db
from app.db.models import Execution, ExecutionStatus, User
from app.schemas import PnLSummary, PositionOut

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/dashboard", tags=["dashboard"])


@router.get("/pnl", response_model=PnLSummary)
def get_pnl(
    user: User = Depends(get_current_user), db: Session = Depends(get_db)
) -> PnLSummary:
    rows = db.query(Execution).filter(Execution.user_id == user.id).all()
    total = len(rows)
    filled = sum(1 for r in rows if r.status == ExecutionStatus.filled)
    rejected = sum(1 for r in rows if r.status == ExecutionStatus.rejected)
    errors = sum(1 for r in rows if r.status == ExecutionStatus.error)
    pending = sum(1 for r in rows if r.status == ExecutionStatus.pending)
    # Realized PnL needs broker close events; placeholder until then.
    return PnLSummary(
        total_executions=total,
        filled=filled,
        rejected=rejected,
        errors=errors,
        pending=pending,
        realized_pnl=0.0,
    )


@router.get("/positions", response_model=list[PositionOut])
async def get_positions(user: User = Depends(get_current_user)) -> list[PositionOut]:
    if not user.tradelocker_token or not user.tradelocker_account_id:
        return []
    token = decrypt(user.tradelocker_token)
    if not token:
        return []
    client = TradeLockerClient(env=user.tradelocker_env or "demo")
    try:
        raw = await client.get_positions(
            user.tradelocker_account_id, token, user.tradelocker_acc_num or "1"
        )
    except TradeLockerError as exc:
        logger.info("positions lookup failed: %s", exc)
        return []

    out: list[PositionOut] = []
    for p in raw or []:
        if not isinstance(p, dict):
            continue
        out.append(
            PositionOut(
                instrument=p.get("instrument") or p.get("symbol"),
                side=p.get("side"),
                quantity=p.get("qty") or p.get("quantity"),
                avg_price=p.get("avgPrice") or p.get("openPrice"),
                unrealized_pnl=p.get("unrealizedPnl") or p.get("pnl"),
                raw=p,
            )
        )
    return out
