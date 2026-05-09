"""Abstract Strategy base class + shared StrategySignal dataclass."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional

import pandas as pd


@dataclass
class StrategySignal:
    """A trade idea emitted by a Strategy on bar close.

    All prices are in instrument quote currency. qty is the BASE lot size
    before per-user risk scaling (risk_engine.compute_user_lot scales it).
    """

    symbol: str
    side: str  # "buy" | "sell"
    entry_price: float
    stop_loss: float
    take_profit: float
    qty: float = 0.01

    # Forecast metadata (post-mortem and feedback loop)
    forecast_drift: float = 0.0  # mean drift in ATR units over horizon
    forecast_confidence: float = 0.0  # |drift| / forecast σ
    threshold: float = 1.5  # confidence threshold at time of signal

    # Free-form bag for downstream consumers / debugging
    extra: dict = field(default_factory=dict)


class Strategy(ABC):
    """Base class for any strategy that runs on bar close."""

    name: str = "base"
    timeframe: str = "1m"

    @abstractmethod
    async def on_bar(
        self, symbol: str, bars: pd.DataFrame
    ) -> Optional[StrategySignal]:
        """Called once per bar close per symbol.

        Args:
            symbol: e.g. "BTCUSD"
            bars:   DataFrame with columns [open, high, low, close, volume],
                    indexed by timestamp (ascending). At least ~16 rows recommended.

        Returns:
            A StrategySignal if a trade should fire, else None.
        """
        raise NotImplementedError
