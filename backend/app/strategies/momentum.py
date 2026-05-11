"""LaT-PFN momentum strategy.

On bar close:
  1. Compute ATR(14) from recent bars.
  2. Get a 12-bar forecast {mean, std} from LaTPFNClient.
  3. drift = (mean[-1] - current_price) / atr   (drift in ATR units)
  4. confidence = |drift| / (avg_std / atr)     (drift as multiple of forecast σ)
  5. If confidence > threshold → fire signal.
     side: buy if drift > 0 else sell
     SL:   current ∓ 1 ATR (against direction)
     TP:   forecast endpoint clipped to ±3 ATR
     qty:  base 0.01 (risk_engine scales per user)
"""
from __future__ import annotations

import logging
from typing import Optional

import numpy as np
import pandas as pd

from app.strategies.base import Strategy, StrategySignal
from app.strategies.latpfn_client import LaTPFNClient

logger = logging.getLogger(__name__)


def compute_atr(bars: pd.DataFrame, period: int = 14) -> float:
    """Wilder-style ATR using simple average of true ranges over `period` bars.

    Returns the most recent ATR value as a float. Pure-numpy, no extra deps.
    """
    if len(bars) < 2:
        return float("nan")
    high = bars["high"].to_numpy(dtype=float)
    low = bars["low"].to_numpy(dtype=float)
    close = bars["close"].to_numpy(dtype=float)
    prev_close = np.concatenate(([close[0]], close[:-1]))
    tr = np.maximum.reduce(
        [
            high - low,
            np.abs(high - prev_close),
            np.abs(low - prev_close),
        ]
    )
    if len(tr) >= period:
        return float(np.mean(tr[-period:]))
    return float(np.mean(tr))


class LatPFNMomentumStrategy(Strategy):
    name = "latpfn_momentum"

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
        # Lowered default 1.5σ → 0.8σ to align with the balanced appetite
        # preset that ships as the new default risk_appetite.
        self.threshold = float(threshold)
        self.atr_period = int(atr_period)
        self.n_predict = int(n_predict)
        self.base_qty = float(base_qty)
        self.target_rr = float(target_rr)

    async def on_bar(
        self, symbol: str, bars: pd.DataFrame
    ) -> Optional[StrategySignal]:
        if bars is None or len(bars) < max(self.atr_period + 1, 16):
            return None

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

        # Drift in ATR units over the forecast horizon
        mean_drift = (float(mean[-1]) - current) / atr
        # Avg forecast σ in ATR units (always positive)
        sigma_atr = float(np.mean(std)) / atr
        sigma_atr = max(sigma_atr, 1e-6)
        confidence = abs(mean_drift) / sigma_atr

        if confidence <= self.threshold:
            return None

        side = "buy" if mean_drift > 0 else "sell"

        # Confidence-scaled TP/SL via the shared tp_scaler. See
        # app.strategies.tp_scaler.compute_tp_sl for the σ→% curve.
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
                "atr": float(atr),
                "forecast_mean_endpoint": float(mean[-1]),
                "forecast_avg_std": float(np.mean(std)),
                "n_predict": self.n_predict,
                "tp_pct": float(levels.tp_pct),
                "rr": float(levels.rr),
            },
        )
