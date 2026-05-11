"""LaT-PFN Quant Strategy — pyramiding + scale-out + trailing SL.

This strategy reuses the LaT-PFN momentum forecast as its signal source
but extends `LatPFNMomentumStrategy` with active trade management:

  Entry            base lot × aggression
  Scale-IN         +0.5R favorable AND forecast still confirms → add another leg
  Scale-OUT        +1.0R hit → close 50% (partial close) + move SL to break-even
  Trail SL         after partial close, ratchet SL by 1×ATR
  Hard exit        TP_final, drawdown > 2×ATR, opposite forecast > 1.5σ

The strategy returns one of three signal kinds via the `kind` field on
StrategySignal.extra:
  - "entry"        — open a new cohort
  - "scale_in"     — add a leg to an existing cohort
  - "manage"       — partial close / SL update / exit (driven by TradeManager.evaluate)

For "manage" signals the runner consults the TradeManager directly; the
on_bar() hook only emits new entries and scale-in candidates. Active
management runs every tick from the runner's evaluate_open_cohorts() loop.
"""
from __future__ import annotations

import logging
from typing import Optional

import numpy as np
import pandas as pd

from app.strategies.base import Strategy, StrategySignal
from app.strategies.latpfn_client import LaTPFNClient
from app.strategies.momentum import compute_atr

logger = logging.getLogger(__name__)


class LatPFNQuantStrategy(Strategy):
    name = "latpfn_quant"

    def __init__(
        self,
        bot_id: int,
        timeframe: str,
        latpfn_client: LaTPFNClient,
        threshold: float = 0.8,
        atr_period: int = 14,
        n_predict: int = 12,
        base_qty: float = 0.01,
        target_rr: float = 1.5,
    ) -> None:
        self.bot_id = bot_id
        self.timeframe = timeframe
        self.latpfn_client = latpfn_client
        # Lowered default threshold from 1.5 → 0.8 to match the balanced
        # appetite preset (= the new default risk_appetite). Conservative
        # accounts get 1.5σ via the appetite preset; aggressive gets 0.3σ.
        self.threshold = float(threshold)
        self.atr_period = int(atr_period)
        self.n_predict = int(n_predict)
        self.base_qty = float(base_qty)
        # Target reward-to-risk. SL distance is derived from this so a
        # 5% TP target at 1.5:1 means a 3.33% SL. ATR-floor still applies.
        self.target_rr = float(target_rr)
        # Optional hook — embedded callers that bypass QuantRunner can attach
        # a TradeManager so on_bar() runs the trailing ratchet directly.
        self._trade_manager = None

    def attach_trade_manager(self, trade_manager) -> None:
        """Attach a TradeManager so on_bar() runs the trailing ratchet."""
        self._trade_manager = trade_manager

    async def manage_open_cohorts(
        self, symbol: str, current_price: float, trade_manager=None
    ) -> list[dict]:
        """Run per-tick trailing ratchet for any cohorts in the partial state.

        Returns the list of leg-update dicts produced by
        TradeManager.update_trailing_ratchet (empty if nothing moved).
        """
        tm = trade_manager or self._trade_manager
        if tm is None:
            return []
        try:
            return await tm.update_trailing_ratchet(symbol, current_price)
        except Exception as exc:  # pragma: no cover — defensive
            logger.warning("trailing ratchet failed for %s: %s", symbol, exc)
            return []

    async def on_bar(
        self, symbol: str, bars: pd.DataFrame
    ) -> Optional[StrategySignal]:
        """Emit an entry signal when forecast confidence > threshold.

        Scale-in / scale-out / trail SL are NOT emitted here — they are
        handled by the runner's per-tick `evaluate_open_cohorts()` call
        through TradeManager.evaluate(). However if a TradeManager has been
        attached via attach_trade_manager(), the trailing ratchet runs here
        on the latest bar close so partial cohorts ratchet up tick-by-tick.
        """
        if bars is None or len(bars) < max(self.atr_period + 1, 16):
            return None

        # Trailing ratchet — only fires if a TradeManager is attached.
        if self._trade_manager is not None and len(bars):
            try:
                current_price = float(bars["close"].iloc[-1])
                await self._trade_manager.update_trailing_ratchet(
                    symbol, current_price
                )
            except Exception as exc:  # pragma: no cover — defensive
                logger.warning("on_bar ratchet failed for %s: %s", symbol, exc)

        atr = compute_atr(bars, self.atr_period)
        if not np.isfinite(atr) or atr <= 0:
            return None

        try:
            f = await self.latpfn_client.forecast(bars, n_predict=self.n_predict)
        except Exception as exc:
            logger.warning("forecast failed for %s: %s", symbol, exc)
            return None

        mean = np.asarray(f["mean"], dtype=float)
        std = np.asarray(f["std"], dtype=float)
        current = float(f["current_price"])
        if mean.size == 0 or std.size == 0:
            return None

        mean_drift = (float(mean[-1]) - current) / atr
        sigma_atr = float(np.mean(std)) / atr
        sigma_atr = max(sigma_atr, 1e-6)
        confidence = abs(mean_drift) / sigma_atr

        if confidence <= self.threshold:
            return None

        side = "buy" if mean_drift > 0 else "sell"

        # Confidence-scaled TP/SL. The legacy fixed ATR-clipped TP capped
        # strong signals at 3×ATR even when the forecast suggested a much
        # bigger move. The scaler maps σ → target % and derives SL from
        # the user's R:R (defaulting to 1.5:1 at the strategy level — the
        # runner can override per-user once we wire risk_appetite end-to-end).
        from app.strategies.tp_scaler import compute_tp_sl
        levels = compute_tp_sl(
            side=side,
            current_price=current,
            atr=atr,
            confidence=confidence,
            target_rr=getattr(self, "target_rr", 1.5),
        )
        sl = levels.stop_loss
        tp = levels.take_profit

        return StrategySignal(
            symbol=symbol,
            side=side,
            entry_price=current,
            stop_loss=float(sl),
            take_profit=float(tp),
            qty=self.base_qty,
            forecast_drift=float(mean_drift),
            forecast_confidence=float(confidence),
            threshold=self.threshold,
            extra={
                "kind": "entry",
                "atr": float(atr),
                "forecast_mean_endpoint": float(mean[-1]),
                "forecast_avg_std": float(np.mean(std)),
                "n_predict": self.n_predict,
                "strategy": "latpfn_quant",
                "tp_pct": float(levels.tp_pct),
                "rr": float(levels.rr),
            },
        )

    async def forecast_view(self, bars: pd.DataFrame) -> Optional[dict]:
        """Return current drift/confidence without entry filtering — used for
        per-tick management decisions (forecast reversal check)."""
        if bars is None or len(bars) < self.atr_period + 1:
            return None
        atr = compute_atr(bars, self.atr_period)
        if not np.isfinite(atr) or atr <= 0:
            return None
        try:
            f = await self.latpfn_client.forecast(bars, n_predict=self.n_predict)
        except Exception:
            return None
        mean = np.asarray(f["mean"], dtype=float)
        std = np.asarray(f["std"], dtype=float)
        current = float(f["current_price"])
        if mean.size == 0 or std.size == 0:
            return None
        drift = (float(mean[-1]) - current) / atr
        sigma_atr = max(float(np.mean(std)) / atr, 1e-6)
        confidence = abs(drift) / sigma_atr
        return {
            "drift": float(drift),
            "confidence": float(confidence),
            "atr": float(atr),
            "current_price": current,
        }
