"""Strategy API — start/stop runners, fetch status, fetch equity curve."""
from __future__ import annotations

import logging
from typing import Optional, Union

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
    runner: Union[StrategyRunner, QuantRunner]
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


@router.get("/analysis")
def analysis(
    bot_id: int,
    timeframe: str = "1m",
    limit: int = 200,
    before_id: int | None = None,
    db: Session = Depends(get_db),
) -> dict:
    """Per-tick decision log — pre-decision visibility into the runner.

    Returns most-recent rows first. Use `before_id` to paginate backward
    (load older entries when user scrolls). Hard cap: 500 per request.
    """
    from app.db.models import StrategyTickLog
    import json as _json

    limit = min(max(int(limit), 1), 500)
    q = (
        db.query(StrategyTickLog)
        .filter(
            StrategyTickLog.bot_id == bot_id,
            StrategyTickLog.timeframe == timeframe,
        )
    )
    if before_id is not None:
        q = q.filter(StrategyTickLog.id < int(before_id))
    rows = q.order_by(StrategyTickLog.id.desc()).limit(limit).all()
    items = []
    for r in rows:
        try:
            extra = _json.loads(r.extra or "{}")
        except Exception:
            extra = {}
        items.append({
            "id": r.id,
            "tick_at": r.tick_at.isoformat(),
            "symbol": r.symbol,
            "current_price": r.current_price,
            "forecast_drift": r.forecast_drift,
            "forecast_std": r.forecast_std,
            "forecast_confidence": r.forecast_confidence,
            "threshold": r.threshold,
            "decision": r.decision,
            "reason": r.reason,
            "extra": extra,
        })
    return {
        "bot_id": bot_id,
        "timeframe": timeframe,
        "items": items,
        "next_before_id": items[-1]["id"] if items else None,
    }


# ---------------------------------------------------------------------
# On-demand analysis — "what would the bot decide if it ticked NOW"
# ---------------------------------------------------------------------

class AnalyzeReq(BaseModel):
    bot_id: int
    timeframe: str = "1m"


@router.post("/analyze")
async def analyze_now(req: AnalyzeReq, db: Session = Depends(get_db)) -> dict:
    """Run a one-shot LaT-PFN analysis over the bot's instruments.

    Does NOT place trades, does NOT spawn a runner, does NOT write to DB.
    It's the same forecast call the runner does every 60s, exposed as a
    button so users can sanity-check before pressing START or see a fresh
    score on demand.

    Returns: per-instrument {symbol, confidence, drift, direction, entry,
    stop_loss, take_profit, tp_pct, rr, fits_threshold}. Symbols where the
    forecast fails (broker error, low-data, etc.) are still returned with
    error=<reason>.
    """
    import asyncio
    import os
    import time

    from app.api.users import get_or_create_user as _gocu  # noqa: F401 (re-export check)
    from app.core.crypto import decrypt
    from app.core.tradelocker_client import TradeLockerClient
    from app.db.models import StrategyState, User
    from app.strategies.data_feed import BarFetcher
    from app.strategies.latpfn_client import LaTPFNClient
    from app.strategies.momentum import compute_atr
    from app.strategies.tp_scaler import compute_tp_sl

    bot = db.get(Bot, req.bot_id)
    if bot is None or not getattr(bot, "is_active", True):
        raise HTTPException(404, "bot not found or inactive")

    # Only LaT-PFN strategies have a confidence concept. Pine bots are
    # event-driven (webhook) and there's no "current view" to sample.
    if bot.strategy_type not in (StrategyType.latpfn_momentum, StrategyType.latpfn_quant):
        raise HTTPException(
            400,
            f"on-demand analysis only supported for LaT-PFN bots; "
            f"got {bot.strategy_type.value}",
        )

    # Resolve the calling user via the strategy-state config or default to
    # the first connected user. The existing /start route relies on the
    # caller passing user_emails; here we just analyze for whichever user
    # has a TradeLocker session that can fetch bars.
    user = (
        db.query(User)
        .filter(User.tradelocker_token.isnot(None), User.tradelocker_account_id.isnot(None))
        .first()
    )
    if user is None:
        raise HTTPException(400, "no user with a connected TradeLocker session")

    token = decrypt(user.tradelocker_token)
    if not token:
        raise HTTPException(400, "broker token unreadable")

    # Threshold defaults to the user's risk_appetite preset; the runner
    # uses the same mapping. Conservative=1.5σ, balanced=0.8σ, aggressive=0.3σ.
    appetite_threshold = {"conservative": 1.5, "balanced": 0.8, "aggressive": 0.3}
    threshold = appetite_threshold.get(user.risk_appetite or "balanced", 0.8)
    target_rr = {"conservative": 2.0, "balanced": 1.5, "aggressive": 1.0}.get(
        user.risk_appetite or "balanced", 1.5
    )

    state = (
        db.query(StrategyState)
        .filter(StrategyState.bot_id == req.bot_id, StrategyState.timeframe == req.timeframe)
        .first()
    )
    if state and state.confidence_threshold:
        # Per-bot override beats the appetite default.
        threshold = float(state.confidence_threshold)

    # Parse the bot's instrument set.
    symbols = [s.strip().upper() for s in (bot.instruments_csv or "").split(",") if s.strip()]
    if not symbols:
        raise HTTPException(400, "bot has no instruments configured")

    # Build broker + forecast clients fresh — these don't share state with
    # the live runner so the on-demand call doesn't affect open positions.
    tl_client = TradeLockerClient(env=user.tradelocker_env or "demo")
    # account_id is non-None here by the filter above; assert narrows for mypy.
    assert user.tradelocker_account_id is not None
    bars_fetcher = BarFetcher(
        client=tl_client,
        account_id=user.tradelocker_account_id,
        token=token,
        acc_num=user.tradelocker_acc_num or "1",
    )
    latpfn = LaTPFNClient(endpoint_url=os.getenv("LATPFN_ENDPOINT_URL") or None)

    async def _one_symbol(symbol: str) -> dict:
        t0 = time.monotonic()
        try:
            bars = await bars_fetcher.fetch_bars(symbol, timeframe=req.timeframe, count=240)
            if bars is None or len(bars) < 30:
                return {"symbol": symbol, "error": "not enough bars"}

            atr = await asyncio.to_thread(compute_atr, bars, 14)
            if not atr or atr <= 0:
                return {"symbol": symbol, "error": "ATR computation failed"}

            f = await latpfn.forecast(bars, n_predict=12)
            mean_endpoint = float(f["mean"][-1])
            current = float(f["current_price"])
            mean_drift = (mean_endpoint - current) / atr
            sigma_atr = sum(f["std"]) / max(len(f["std"]), 1) / atr
            sigma_atr = max(sigma_atr, 1e-6)
            confidence = abs(mean_drift) / sigma_atr
            direction = "buy" if mean_drift > 0 else "sell"
            fits_threshold = confidence >= threshold

            levels = compute_tp_sl(
                side=direction,
                current_price=current,
                atr=atr,
                confidence=confidence,
                target_rr=target_rr,
            )

            return {
                "symbol": symbol,
                "confidence": round(confidence, 3),
                "drift_atr": round(mean_drift, 3),
                "direction": direction,
                "entry": levels.entry,
                "stop_loss": levels.stop_loss,
                "take_profit": levels.take_profit,
                "tp_pct": round(levels.tp_pct, 2),
                "rr": round(levels.rr, 2),
                "fits_threshold": fits_threshold,
                "atr": round(atr, 6),
                "elapsed_ms": int((time.monotonic() - t0) * 1000),
            }
        except Exception as exc:  # noqa: BLE001 — analysis must never crash
            logger.warning("analyze_now %s failed: %s", symbol, exc)
            return {
                "symbol": symbol,
                "error": str(exc)[:240],
                "elapsed_ms": int((time.monotonic() - t0) * 1000),
            }

    # Run all symbols concurrently — they share httpx clients but each
    # call is independent.
    started = time.monotonic()
    items = await asyncio.gather(*[_one_symbol(s) for s in symbols])
    total_ms = int((time.monotonic() - started) * 1000)

    return {
        "bot_id": req.bot_id,
        "bot_name": bot.name,
        "timeframe": req.timeframe,
        "threshold": threshold,
        "target_rr": target_rr,
        "risk_appetite": user.risk_appetite or "balanced",
        "user_email": user.email,
        "items": items,
        "elapsed_ms": total_ms,
    }
