"""Isolation harness API — register dedicated demo accounts per strategy,
start/stop isolated runners, and compare clean per-account performance.

Provisioning model (operator-confirmed): many TradeLocker sub-accounts
under ONE Genesis FX login. The caller's saved broker token is reused; a
StrategyAccount only pins which sub-account a given bot trades on. Two DB
unique constraints guarantee no strategy shares an account, so equity
curves never cross-contaminate.
"""
from __future__ import annotations

import logging
import math
import os
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.api.users import get_current_user
from app.core.crypto import decrypt
from app.core.tradelocker_client import TradeLockerClient, TradeLockerError
from app.db.database import SessionLocal, get_db
from app.db.models import (
    Bot,
    StrategyAccount,
    TradeOutcome,
    User,
)
from app.strategies.isolation import IsolatedRunner, get_iso_runner
from app.strategies.registry import is_registered

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/isolation", tags=["isolation"])


def _bot_strategy_key(bot: Bot) -> str:
    """Dispatch key for a bot — partner slug if set, else the enum value."""
    return bot.strategy_slug or bot.strategy_type.value


# --------------------------------------------------------------------- #
# schemas
# --------------------------------------------------------------------- #
class RegisterAccountReq(BaseModel):
    bot_id: int
    tradelocker_account_id: str
    tradelocker_acc_num: str = "1"
    label: str = ""
    timeframe: str = "1m"
    start_offset_seconds: int = 0


class BotRef(BaseModel):
    bot_id: int


# --------------------------------------------------------------------- #
# account provisioning
# --------------------------------------------------------------------- #
@router.get("/available-accounts")
async def available_accounts(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    """Sub-accounts on the caller's broker login, flagged with which are
    already bound to a strategy (so the operator picks unbound ones)."""
    if not user.tradelocker_token:
        raise HTTPException(400, "no broker linked")
    token = decrypt(user.tradelocker_token)
    if not token:
        raise HTTPException(400, "broker token unreadable")

    client = TradeLockerClient(env=user.tradelocker_env or "demo")
    try:
        accounts = await client.list_all_accounts(token)
    except TradeLockerError as exc:
        logger.info("isolation available-accounts upstream error: %s", exc)
        raise HTTPException(
            502, "Broker not responding. Re-/connect to refresh the session."
        )

    bound = {
        sa.tradelocker_account_id: sa.bot_id
        for sa in db.query(StrategyAccount).all()
    }
    return {
        "env": user.tradelocker_env or "demo",
        "accounts": [
            {
                "account_id": str(a.get("id")),
                "acc_num": str(a.get("accNum")),
                "name": a.get("name"),
                "balance": float(a.get("accountBalance", 0) or 0),
                "currency": a.get("currency"),
                "bound_to_bot_id": bound.get(str(a.get("id"))),
            }
            for a in accounts
        ],
    }


@router.post("/accounts")
async def register_account(
    req: RegisterAccountReq,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    bot = db.get(Bot, req.bot_id)
    if bot is None or not getattr(bot, "is_active", True):
        raise HTTPException(404, "bot not found or inactive")
    if not is_registered(_bot_strategy_key(bot)):
        raise HTTPException(
            400,
            f"isolation supports registered strategies only; "
            f"strategy {_bot_strategy_key(bot)!r} is not loaded (webhook-driven)",
        )
    if not user.tradelocker_token:
        raise HTTPException(400, "no broker linked")
    token = decrypt(user.tradelocker_token)
    if not token:
        raise HTTPException(400, "broker token unreadable")

    # Verify the sub-account actually exists on this login.
    client = TradeLockerClient(env=user.tradelocker_env or "demo")
    try:
        accounts = await client.list_all_accounts(token)
    except TradeLockerError as exc:
        logger.info("isolation register upstream error: %s", exc)
        raise HTTPException(502, "Broker not responding. Re-/connect.")
    match = next(
        (a for a in accounts if str(a.get("id")) == req.tradelocker_account_id),
        None,
    )
    if match is None:
        raise HTTPException(
            400,
            f"account {req.tradelocker_account_id} not found on your login",
        )

    # Enforce isolation invariants explicitly for friendly errors (the DB
    # unique constraints are the real guarantee).
    if (
        db.query(StrategyAccount)
        .filter(StrategyAccount.bot_id == req.bot_id)
        .first()
    ):
        raise HTTPException(409, "bot already has a dedicated account")
    if (
        db.query(StrategyAccount)
        .filter(
            StrategyAccount.tradelocker_account_id
            == req.tradelocker_account_id
        )
        .first()
    ):
        raise HTTPException(
            409, "that demo account is already bound to another strategy"
        )

    sa = StrategyAccount(
        bot_id=req.bot_id,
        user_id=user.id,
        label=req.label or f"{bot.name} (isolated)",
        tradelocker_account_id=req.tradelocker_account_id,
        tradelocker_acc_num=str(match.get("accNum") or req.tradelocker_acc_num),
        tradelocker_env=user.tradelocker_env or "demo",
        timeframe=req.timeframe,
        start_offset_seconds=max(0, int(req.start_offset_seconds)),
    )
    db.add(sa)
    db.commit()
    db.refresh(sa)
    return _account_dict(sa, bot)


@router.get("/accounts")
def list_accounts(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    """List the caller's isolated strategy bindings.

    Scoped to user_id so a session can only see bindings created with
    their own broker credentials. Without this scope, any authenticated
    session can enumerate every other user's strategy accounts.
    """
    rows = (
        db.query(StrategyAccount)
        .filter(StrategyAccount.user_id == user.id)
        .order_by(StrategyAccount.id.asc())
        .all()
    )
    out = []
    for sa in rows:
        bot = db.get(Bot, sa.bot_id)
        out.append(_account_dict(sa, bot))
    return {"accounts": out}


@router.delete("/accounts/{account_id}")
def delete_account(
    account_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    """Delete an isolated binding. Owner-only — non-owners receive 404
    (not 403) so account IDs cannot be enumerated by probing."""
    sa = db.get(StrategyAccount, account_id)
    if sa is None or sa.user_id != user.id:
        raise HTTPException(404, "binding not found")
    runner = get_iso_runner(sa.bot_id)
    if runner and runner.task and not runner.task.done():
        raise HTTPException(
            409, "stop the isolated runner before deleting its binding"
        )
    db.delete(sa)
    db.commit()
    return {"status": "deleted", "account_id": account_id}


# --------------------------------------------------------------------- #
# runner lifecycle
# --------------------------------------------------------------------- #
@router.post("/start")
async def start(
    req: BotRef,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    """Start the isolated runner for the caller's binding on this bot.

    Filters StrategyAccount by user_id — a session cannot start a
    runner against another user's strategy account, even by guessing
    its bot_id.
    """
    sa = (
        db.query(StrategyAccount)
        .filter(
            StrategyAccount.bot_id == req.bot_id,
            StrategyAccount.user_id == user.id,
            StrategyAccount.is_active.is_(True),
        )
        .first()
    )
    if sa is None:
        raise HTTPException(
            404, "no dedicated account registered for this bot"
        )
    try:
        runner = await IsolatedRunner.start(
            SessionLocal,
            sa.id,
            latpfn_endpoint=os.getenv("LATPFN_ENDPOINT_URL") or None,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return {
        "status": "started",
        "bot_id": runner.bot_id,
        "timeframe": runner.timeframe,
        "symbols": runner.symbols,
        "strategy_account_id": sa.id,
        "task_alive": runner.task is not None and not runner.task.done(),
    }


@router.post("/stop")
async def stop(
    req: BotRef,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    """Stop the isolated runner. Only the owner of the bound StrategyAccount
    may stop it — non-owners get 404 (not 403) for the same reason as
    delete_account: avoid enumeration."""
    sa = (
        db.query(StrategyAccount)
        .filter(
            StrategyAccount.bot_id == req.bot_id,
            StrategyAccount.user_id == user.id,
        )
        .first()
    )
    if sa is None:
        raise HTTPException(404, "no dedicated account for this bot")
    runner = get_iso_runner(req.bot_id)
    if runner is None:
        return {"status": "not_running", "bot_id": req.bot_id}
    await runner.stop()
    return {"status": "stopped", "bot_id": req.bot_id}


# --------------------------------------------------------------------- #
# clean per-account performance
# --------------------------------------------------------------------- #
@router.get("/equity")
def equity(
    bot_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    """Cumulative R / PnL curve for the caller's isolated strategy on this
    bot. Filtered by strategy_account_id AND user_id so equity curves
    are scoped to the requesting owner — sessions cannot read another
    user's clean equity series by guessing bot_id."""
    sa = (
        db.query(StrategyAccount)
        .filter(
            StrategyAccount.bot_id == bot_id,
            StrategyAccount.user_id == user.id,
        )
        .first()
    )
    if sa is None:
        raise HTTPException(404, "no dedicated account for this bot")
    rows = (
        db.query(TradeOutcome)
        .filter(TradeOutcome.strategy_account_id == sa.id)
        .order_by(TradeOutcome.closed_at.asc())
        .all()
    )
    cum_r = cum_pnl = 0.0
    timestamps: list[str] = []
    r_curve: list[float] = []
    pnl_curve: list[float] = []
    for r in rows:
        cum_r += float(r.r_multiple)
        cum_pnl += float(r.pnl_usd)
        timestamps.append(r.closed_at.isoformat())
        r_curve.append(cum_r)
        pnl_curve.append(cum_pnl)
    return {
        "bot_id": bot_id,
        "strategy_account_id": sa.id,
        "timestamps": timestamps,
        "cumulative_r": r_curve,
        "cumulative_pnl_usd": pnl_curve,
        "total_trades": len(rows),
    }


@router.get("/compare")
def compare(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    """Side-by-side clean stats across the caller's isolated strategies
    only. Scoped to user_id so users cannot compare across other users'
    accounts."""
    rows = (
        db.query(StrategyAccount)
        .filter(StrategyAccount.user_id == user.id)
        .order_by(StrategyAccount.id.asc())
        .all()
    )
    results = []
    for sa in rows:
        bot = db.get(Bot, sa.bot_id)
        outcomes = (
            db.query(TradeOutcome)
            .filter(TradeOutcome.strategy_account_id == sa.id)
            .order_by(TradeOutcome.closed_at.asc())
            .all()
        )
        results.append(
            {
                **_account_dict(sa, bot),
                "stats": _compute_stats(outcomes),
            }
        )
    return {"strategies": results}


# --------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------- #
def _account_dict(sa: StrategyAccount, bot: Optional[Bot]) -> dict:
    runner = get_iso_runner(sa.bot_id)
    return {
        "id": sa.id,
        "bot_id": sa.bot_id,
        "bot_name": bot.name if bot else None,
        "strategy_type": bot.strategy_type.value if bot else None,
        "label": sa.label,
        "tradelocker_account_id": sa.tradelocker_account_id,
        "tradelocker_acc_num": sa.tradelocker_acc_num,
        "env": sa.tradelocker_env,
        "timeframe": sa.timeframe,
        "start_offset_seconds": sa.start_offset_seconds,
        "is_active": sa.is_active,
        "runner_alive": bool(
            runner and runner.task and not runner.task.done()
        ),
    }


def _compute_stats(outcomes: list[TradeOutcome]) -> dict:
    n = len(outcomes)
    if n == 0:
        return {
            "total_trades": 0,
            "win_rate": 0.0,
            "total_pnl_usd": 0.0,
            "total_r": 0.0,
            "avg_r": 0.0,
            "profit_factor": 0.0,
            "sharpe": 0.0,
            "max_drawdown_r": 0.0,
        }
    rs = [float(o.r_multiple) for o in outcomes]
    pnls = [float(o.pnl_usd) for o in outcomes]
    wins = [p for p in pnls if p > 0]
    gross_win = sum(wins)
    gross_loss = abs(sum(p for p in pnls if p < 0))
    mean_r = sum(rs) / n
    var = sum((x - mean_r) ** 2 for x in rs) / n
    std = math.sqrt(var)

    # Max drawdown on the cumulative-R curve.
    peak = cum = 0.0
    max_dd = 0.0
    for x in rs:
        cum += x
        peak = max(peak, cum)
        max_dd = min(max_dd, cum - peak)

    return {
        "total_trades": n,
        "win_rate": round(len(wins) / n, 4),
        "total_pnl_usd": round(sum(pnls), 2),
        "total_r": round(sum(rs), 3),
        "avg_r": round(mean_r, 4),
        "profit_factor": round(gross_win / gross_loss, 3)
        if gross_loss > 1e-9
        else (float("inf") if gross_win > 0 else 0.0),
        "sharpe": round(mean_r / std, 3) if std > 1e-9 else 0.0,
        "max_drawdown_r": round(max_dd, 3),
    }
