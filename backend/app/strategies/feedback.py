"""FeedbackAdjuster — auto-tunes confidence_threshold from rolling stats.

Rules:
  - total_trades < 20             → ("warmup", no change)
  - max_drawdown_pct > 10         → ("pause", +1.0 to threshold, pause 30 min)
  - win_rate < 0.45               → ("tighten", +0.5 to threshold)
  - win_rate > 0.60 AND PF > 1.5  → ("loosen", −0.2, floor 0.8)
  - else                          → ("hold", no change)
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy.orm import Session

from app.db.models import StrategyState

logger = logging.getLogger(__name__)


PAUSE_MINUTES = 30
THRESHOLD_FLOOR = 0.8


class FeedbackAdjuster:
    def __init__(self, db: Session, bot_id: int) -> None:
        self.db = db
        self.bot_id = bot_id

    async def adjust(
        self,
        stats: dict,
        current_threshold: float,
        timeframe: Optional[str] = None,
    ) -> tuple[float, str]:
        """Return (new_threshold, action).

        If `timeframe` is provided, also persists threshold (and pause) on
        the matching StrategyState row.
        """
        total = int(stats.get("total_trades", 0))
        if total < 20:
            return current_threshold, "warmup"

        win_rate = float(stats.get("win_rate", 0.0))
        profit_factor = float(stats.get("profit_factor", 0.0))
        max_dd_pct = float(stats.get("max_drawdown_pct", 0.0))

        new_threshold = current_threshold
        action = "hold"

        if max_dd_pct > 10.0:
            new_threshold = current_threshold + 1.0
            action = "pause"
        elif win_rate < 0.45:
            new_threshold = current_threshold + 0.5
            action = "tighten"
        elif win_rate > 0.60 and profit_factor > 1.5:
            new_threshold = max(current_threshold - 0.2, THRESHOLD_FLOOR)
            action = "loosen"

        if timeframe and (action != "hold" or new_threshold != current_threshold):
            state = (
                self.db.query(StrategyState)
                .filter(
                    StrategyState.bot_id == self.bot_id,
                    StrategyState.timeframe == timeframe,
                )
                .first()
            )
            if state is not None:
                state.confidence_threshold = float(new_threshold)
                if action == "pause":
                    state.is_running = False
                    state.paused_until = datetime.utcnow() + timedelta(minutes=PAUSE_MINUTES)
                self.db.commit()

        return float(new_threshold), action
