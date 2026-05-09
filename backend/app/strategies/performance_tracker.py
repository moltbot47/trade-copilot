"""PerformanceTracker — rolling stats + snapshot rows.

Reads the most recent N TradeOutcome rows for a bot, computes rolling
performance stats, and writes a PerformanceSnapshot row each time it runs.
"""
from __future__ import annotations

import logging
import math
from datetime import datetime
from typing import Optional

import numpy as np
from sqlalchemy.orm import Session

from app.db.models import PerformanceSnapshot, StrategyState, TradeOutcome

logger = logging.getLogger(__name__)


class PerformanceTracker:
    def __init__(self, db: Session, bot_id: int) -> None:
        self.db = db
        self.bot_id = bot_id

    async def record_trade_close(self, outcome: TradeOutcome) -> None:
        """Persist a closed trade. Caller decides when to recompute stats."""
        if outcome.id is None:
            self.db.add(outcome)
        self.db.commit()

    def compute_rolling_stats(self, window: int = 20) -> dict:
        """Compute stats over the last `window` closed trades.

        Returns dict with: window_size, total_trades, win_rate, profit_factor,
        sharpe, avg_r, max_drawdown_pct, total_pnl_usd.
        """
        rows = (
            self.db.query(TradeOutcome)
            .filter(TradeOutcome.bot_id == self.bot_id)
            .order_by(TradeOutcome.closed_at.desc())
            .limit(window)
            .all()
        )
        n = len(rows)
        if n == 0:
            return self._empty_stats(window)

        # Order chronologically for drawdown
        rows = list(reversed(rows))
        rs = np.array([r.r_multiple for r in rows], dtype=float)
        pnls = np.array([r.pnl_usd for r in rows], dtype=float)

        wins = rs > 0
        win_rate = float(wins.mean()) if n > 0 else 0.0

        gross_win = float(pnls[pnls > 0].sum())
        gross_loss = float(-pnls[pnls < 0].sum())
        profit_factor = (
            gross_win / gross_loss if gross_loss > 1e-9 else (math.inf if gross_win > 0 else 0.0)
        )
        # Cap profit_factor to avoid storing inf in DB
        if not math.isfinite(profit_factor):
            profit_factor = 99.0

        avg_r = float(rs.mean()) if n > 0 else 0.0
        std_r = float(rs.std(ddof=1)) if n > 1 else 0.0
        sharpe = (avg_r / std_r) * math.sqrt(n) if std_r > 1e-9 else 0.0

        # Max drawdown of cumulative R-curve (in R units, then percentage of peak)
        cum_r = np.cumsum(rs)
        peak = np.maximum.accumulate(cum_r)
        # If peak ever <= 0, fall back to absolute drawdown in R
        denom = np.where(np.abs(peak) > 1e-9, np.abs(peak), 1.0)
        dd_pct = ((peak - cum_r) / denom) * 100.0
        max_dd_pct = float(dd_pct.max()) if dd_pct.size else 0.0

        total_pnl = float(pnls.sum())

        return {
            "window_size": window,
            "total_trades": n,
            "win_rate": win_rate,
            "profit_factor": float(profit_factor),
            "sharpe": float(sharpe),
            "avg_r": avg_r,
            "max_drawdown_pct": max_dd_pct,
            "total_pnl_usd": total_pnl,
        }

    def write_snapshot(
        self,
        stats: dict,
        threshold_after: float,
        feedback_action: Optional[str],
    ) -> PerformanceSnapshot:
        snap = PerformanceSnapshot(
            bot_id=self.bot_id,
            snapshot_at=datetime.utcnow(),
            window_size=int(stats.get("window_size", 20)),
            win_rate=float(stats.get("win_rate", 0.0)),
            profit_factor=float(stats.get("profit_factor", 0.0)),
            sharpe=float(stats.get("sharpe", 0.0)),
            avg_r=float(stats.get("avg_r", 0.0)),
            max_drawdown_pct=float(stats.get("max_drawdown_pct", 0.0)),
            total_pnl_usd=float(stats.get("total_pnl_usd", 0.0)),
            total_trades=int(stats.get("total_trades", 0)),
            threshold_after=float(threshold_after),
            feedback_action=feedback_action,
        )
        self.db.add(snap)
        self.db.commit()
        return snap

    def total_outcomes(self) -> int:
        return (
            self.db.query(TradeOutcome)
            .filter(TradeOutcome.bot_id == self.bot_id)
            .count()
        )

    def latest_snapshot(self) -> Optional[PerformanceSnapshot]:
        return (
            self.db.query(PerformanceSnapshot)
            .filter(PerformanceSnapshot.bot_id == self.bot_id)
            .order_by(PerformanceSnapshot.snapshot_at.desc())
            .first()
        )

    @staticmethod
    def _empty_stats(window: int) -> dict:
        return {
            "window_size": window,
            "total_trades": 0,
            "win_rate": 0.0,
            "profit_factor": 0.0,
            "sharpe": 0.0,
            "avg_r": 0.0,
            "max_drawdown_pct": 0.0,
            "total_pnl_usd": 0.0,
        }


def find_state(db: Session, bot_id: int, timeframe: str) -> Optional[StrategyState]:
    return (
        db.query(StrategyState)
        .filter(StrategyState.bot_id == bot_id, StrategyState.timeframe == timeframe)
        .first()
    )
