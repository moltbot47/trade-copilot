"""Strategy API — start/stop runners, fetch status, fetch equity curve."""
from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.db.database import SessionLocal, get_db
from app.db.models import (
    Bot,
    PerformanceSnapshot,
    StrategyState,
    StrategyType,
    TradeOutcome,
)
from app.strategies.runner import QuantRunner, StrategyRunner, get_runner

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/strategy", tags=["strategy"])


class StartReq(BaseModel):
    bot_id: int
    timeframe: str = "1m"
    symbols: list[str]
    user_emails: list[str]
    latpfn_endpoint: Optional[str] = None
    # Optional per-run threshold override (writes to StrategyState before
    # the runner starts). Useful for live demos on calm markets.
    threshold: Optional[float] = None


class StopReq(BaseModel):
    bot_id: int
    timeframe: str = "1m"


@router.post("/start")
async def start(req: StartReq, db: Session = Depends(get_db)) -> dict:
    bot = db.get(Bot, req.bot_id)
    if bot is None:
        raise HTTPException(404, "bot not found")
    if not getattr(bot, "is_active", True):
        raise HTTPException(404, "bot not found or inactive")
    if not req.symbols:
        raise HTTPException(400, "symbols list cannot be empty")
    if not req.user_emails:
        raise HTTPException(400, "user_emails list cannot be empty")

    # Optional threshold override — write to StrategyState before runner spawns.
    if req.threshold is not None:
        if not (0.1 <= req.threshold <= 5.0):
            raise HTTPException(400, "threshold must be between 0.1 and 5.0")
        state = (
            db.query(StrategyState)
            .filter(StrategyState.bot_id == req.bot_id, StrategyState.timeframe == req.timeframe)
            .first()
        )
        if state is None:
            state = StrategyState(
                bot_id=req.bot_id,
                timeframe=req.timeframe,
                is_running=False,
                confidence_threshold=req.threshold,
                max_concurrent=3,
            )
            db.add(state)
        else:
            state.confidence_threshold = req.threshold
        db.commit()

    # Dispatch based on strategy type. TradingView-webhook bots (orb,
    # squeeze, stoch_hook) are NOT launched via this endpoint — they fire
    # from the /webhook/{slug} route on every TV alert.
    if bot.strategy_type == StrategyType.latpfn_momentum:
        runner = await StrategyRunner.start(
            db_session_factory=SessionLocal,
            bot_id=req.bot_id,
            timeframe=req.timeframe,
            symbols=req.symbols,
            user_emails=req.user_emails,
            latpfn_endpoint=req.latpfn_endpoint,
        )
    elif bot.strategy_type == StrategyType.latpfn_quant:
        runner = await QuantRunner.start(
            db_session_factory=SessionLocal,
            bot_id=req.bot_id,
            timeframe=req.timeframe,
            symbols=req.symbols,
            user_emails=req.user_emails,
            latpfn_endpoint=req.latpfn_endpoint,
        )
    else:
        raise HTTPException(
            400,
            f"strategy {bot.strategy_type.value} cannot be started via "
            "/strategy/start (TradingView-webhook driven)",
        )

    return {
        "status": "started",
        "bot_id": runner.bot_id,
        "timeframe": runner.timeframe,
        "symbols": runner.symbols,
        "users": runner.user_emails,
        "task_alive": runner.task is not None and not runner.task.done(),
        "runner_type": type(runner).__name__,
    }


@router.post("/stop")
async def stop(req: StopReq) -> dict:
    runner = get_runner(req.bot_id, req.timeframe)
    if runner is None:
        return {"status": "not_running", "bot_id": req.bot_id, "timeframe": req.timeframe}
    await runner.stop()
    return {"status": "stopped", "bot_id": req.bot_id, "timeframe": req.timeframe}


@router.get("/status")
def status(
    bot_id: int,
    timeframe: str = "1m",
    db: Session = Depends(get_db),
) -> dict:
    state = (
        db.query(StrategyState)
        .filter(StrategyState.bot_id == bot_id, StrategyState.timeframe == timeframe)
        .first()
    )
    if state is None:
        # Return a structured "not initialized" payload — the dashboard
        # should be able to render an empty state without erroring.
        return {
            "state": {
                "bot_id": bot_id,
                "timeframe": timeframe,
                "is_running": False,
                "confidence_threshold": 1.5,
                "max_concurrent": 3,
                "paused_until": None,
                "last_tick_at": None,
                "last_signal_at": None,
                "last_error": None,
                "started_at": None,
            },
            "runner_alive": False,
            "performance": None,
            "latest_snapshot": None,
            "recent_trades": [],
            "recent_outcomes": [],
            "recent_snapshots": [],
        }

    snap = (
        db.query(PerformanceSnapshot)
        .filter(PerformanceSnapshot.bot_id == bot_id)
        .order_by(PerformanceSnapshot.snapshot_at.desc())
        .first()
    )
    outcomes = (
        db.query(TradeOutcome)
        .filter(TradeOutcome.bot_id == bot_id, TradeOutcome.timeframe == timeframe)
        .order_by(TradeOutcome.closed_at.desc())
        .limit(20)
        .all()
    )

    runner = get_runner(bot_id, timeframe)

    perf = {
        "snapshot_at": snap.snapshot_at.isoformat() if snap else None,
        "window_size": snap.window_size if snap else 20,
        "win_rate": snap.win_rate if snap else 0.0,
        "profit_factor": snap.profit_factor if snap else 0.0,
        "sharpe": snap.sharpe if snap else 0.0,
        "avg_r": snap.avg_r if snap else 0.0,
        "max_drawdown_pct": snap.max_drawdown_pct if snap else 0.0,
        "total_pnl_usd": snap.total_pnl_usd if snap else 0.0,
        "total_trades": snap.total_trades if snap else 0,
        "threshold_after": snap.threshold_after if snap else state.confidence_threshold,
        "feedback_action": snap.feedback_action if snap else None,
    } if snap else None

    trades = [
        {
            "id": o.id,
            "bot_id": o.bot_id,
            "instrument": o.instrument,
            "side": o.side,
            "timeframe": o.timeframe,
            "entry_price": o.entry_price,
            "exit_price": o.exit_price,
            "qty": o.qty,
            "pnl_usd": o.pnl_usd,
            "r_multiple": o.r_multiple,
            "forecast_drift": o.forecast_drift,
            "forecast_confidence": o.forecast_confidence,
            "threshold_at_entry": o.threshold_at_entry,
            "opened_at": o.opened_at.isoformat(),
            "closed_at": o.closed_at.isoformat(),
            "hold_seconds": o.hold_seconds,
        }
        for o in outcomes
    ]

    return {
        "state": {
            "bot_id": state.bot_id,
            "timeframe": state.timeframe,
            "is_running": state.is_running,
            "confidence_threshold": state.confidence_threshold,
            "max_concurrent": state.max_concurrent,
            "paused_until": state.paused_until.isoformat() if state.paused_until else None,
            "last_tick_at": state.last_tick_at.isoformat() if state.last_tick_at else None,
            "last_signal_at": state.last_signal_at.isoformat() if state.last_signal_at else None,
            "last_error": state.last_error,
            "started_at": state.started_at.isoformat() if state.started_at else None,
        },
        "runner_alive": bool(runner and runner.task and not runner.task.done()),
        "performance": perf,                # frontend-aligned
        "latest_snapshot": perf,             # backwards-compat
        "recent_trades": trades,             # frontend-aligned
        "recent_outcomes": trades,           # backwards-compat
        "recent_snapshots": [perf] if perf else [],
    }


@router.get("/equity")
def equity(bot_id: int, db: Session = Depends(get_db)) -> dict:
    """Cumulative R-curve, ascending in time."""
    rows = (
        db.query(TradeOutcome)
        .filter(TradeOutcome.bot_id == bot_id)
        .order_by(TradeOutcome.closed_at.asc())
        .all()
    )
    timestamps = [r.closed_at.isoformat() for r in rows]
    cum_r = 0.0
    cum_pnl = 0.0
    r_curve: list[float] = []
    pnl_curve: list[float] = []
    for r in rows:
        cum_r += float(r.r_multiple)
        cum_pnl += float(r.pnl_usd)
        r_curve.append(cum_r)
        pnl_curve.append(cum_pnl)
    return {
        "bot_id": bot_id,
        "timestamps": timestamps,
        "cumulative_r": r_curve,
        "cumulative_pnl_usd": pnl_curve,
        "total_trades": len(rows),
    }
