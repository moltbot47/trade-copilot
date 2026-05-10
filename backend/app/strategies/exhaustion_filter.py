"""Exhaustion filter — gate entries on wick-rejection + RSI extremes.

Rationale: on a $21 live account the user explicitly asked for a strategy
that *waits* for an overextended move and enters AGAINST it (mean
reversion), rather than chasing breakouts. Pure momentum-only entries
chase strength and tend to enter near local tops/bottoms, which is
exactly the kind of unfavorable fill you can't absorb at 38× leverage.

This module is a *filter*, not a standalone strategy: callers compute
their usual entry signal, then call ``passes_exhaustion(...)`` to decide
whether to fire it. The filter agrees with a momentum signal only when
the prior bar shows wick rejection + an RSI extreme that *aligns with
the proposed side*:

  side == "buy"   →  wants oversold setup: RSI < oversold AND prev bar
                     has long lower wick (rejection of the low)
  side == "sell"  →  wants overbought setup: RSI > overbought AND prev
                     bar has long upper wick (rejection of the high)

Why the alignment check matters: a momentum forecast can call "buy"
even at the top of a parabolic move. We refuse to fire a buy unless the
bar pattern shows the move just stalled (wick rejection at the top is
fine for shorts but here we want a buy → we need rejection at the
*bottom* showing buyers stepped in).
"""
from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd


def compute_rsi(closes: np.ndarray, period: int = 14) -> float:
    """Wilder RSI on a 1-D close series. Returns the most recent value."""
    if len(closes) < period + 1:
        return float("nan")
    deltas = np.diff(closes)
    gains = np.where(deltas > 0, deltas, 0.0)
    losses = np.where(deltas < 0, -deltas, 0.0)
    # Wilder smoothing: simple seed, then EMA-style update
    avg_gain = float(np.mean(gains[:period]))
    avg_loss = float(np.mean(losses[:period]))
    for i in range(period, len(deltas)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def passes_no_chase(
    bars: pd.DataFrame,
    side: str,
    rsi_period: int = 14,
    rsi_chase_high: float = 70.0,
    rsi_chase_low: float = 30.0,
    range_lookback: int = 10,
    range_chase_high: float = 0.85,
    range_chase_low: float = 0.15,
) -> tuple[bool, dict]:
    """No-chase filter — block obvious top-chasing or bottom-chasing entries.

    Allows momentum trades at any "normal" position in the recent range, but
    refuses to fire when the price is already at a local extreme.

    BUY blocked if:
      - RSI > rsi_chase_high (already overbought = chasing the top)
      - OR price > 85% of last `range_lookback` bars' range (fresh high zone)

    SELL blocked if:
      - RSI < rsi_chase_low (already oversold)
      - OR price < 15% of last `range_lookback` bars' range (fresh low zone)

    Returns (allowed, diagnostic_dict).
    """
    if bars is None or len(bars) < max(rsi_period + 2, range_lookback + 1):
        return False, {"reason": "insufficient_bars"}

    closes = bars["close"].to_numpy(dtype=float)
    highs = bars["high"].to_numpy(dtype=float)
    lows = bars["low"].to_numpy(dtype=float)

    rsi = compute_rsi(closes, rsi_period)
    if not np.isfinite(rsi):
        return False, {"reason": "rsi_nan"}

    # Recent range position (0 = at the low, 1 = at the high)
    recent_high = float(np.max(highs[-range_lookback:]))
    recent_low = float(np.min(lows[-range_lookback:]))
    span = recent_high - recent_low
    current = float(closes[-1])
    range_pos = (current - recent_low) / span if span > 0 else 0.5

    diag: dict[str, Any] = {
        "rsi": round(rsi, 2),
        "range_pos": round(range_pos, 3),
        "recent_high": round(recent_high, 2),
        "recent_low": round(recent_low, 2),
    }

    if side == "buy":
        if rsi > rsi_chase_high:
            diag["reason"] = f"chasing_top_rsi={rsi:.1f}>{rsi_chase_high}"
            return False, diag
        if range_pos > range_chase_high:
            diag["reason"] = f"chasing_top_range={range_pos:.2f}>{range_chase_high}"
            return False, diag
        diag["reason"] = "ok_not_chasing_top"
        return True, diag

    if side == "sell":
        if rsi < rsi_chase_low:
            diag["reason"] = f"chasing_bottom_rsi={rsi:.1f}<{rsi_chase_low}"
            return False, diag
        if range_pos < range_chase_low:
            diag["reason"] = f"chasing_bottom_range={range_pos:.2f}<{range_chase_low}"
            return False, diag
        diag["reason"] = "ok_not_chasing_bottom"
        return True, diag

    diag["reason"] = "unknown_side"
    return False, diag


def passes_exhaustion(
    bars: pd.DataFrame,
    side: str,
    rsi_period: int = 14,
    overbought: float = 70.0,
    oversold: float = 30.0,
    min_wick_to_body: float = 1.5,
    max_body_to_range: float = 0.5,
) -> tuple[bool, dict]:
    """Return (allowed, diagnostic_dict).

    A buy is allowed only if:
      - RSI <= oversold (market got pushed down hard)
      - Prior bar's lower wick is >= 1.5× body (sellers got rejected)
      - Prior bar's body is < 50% of total range (not a big trend bar)

    Mirror logic for sell: RSI >= overbought + upper-wick rejection.
    """
    if bars is None or len(bars) < max(rsi_period + 2, 30):
        return False, {"reason": "insufficient_bars"}

    closes = bars["close"].to_numpy(dtype=float)
    rsi = compute_rsi(closes, rsi_period)
    if not np.isfinite(rsi):
        return False, {"reason": "rsi_nan"}

    prev = bars.iloc[-2]
    p_open = float(prev["open"])
    p_high = float(prev["high"])
    p_low = float(prev["low"])
    p_close = float(prev["close"])

    body = abs(p_close - p_open)
    bar_range = p_high - p_low
    if bar_range <= 0:
        return False, {"reason": "doji_range_zero", "rsi": rsi}

    upper_wick = p_high - max(p_close, p_open)
    lower_wick = min(p_close, p_open) - p_low
    body_ratio = body / bar_range
    upper_wick_ratio = (upper_wick / body) if body > 0 else float("inf")
    lower_wick_ratio = (lower_wick / body) if body > 0 else float("inf")

    diag: dict[str, Any] = {
        "rsi": round(rsi, 2),
        "body_ratio": round(body_ratio, 3),
        "upper_wick_ratio": round(upper_wick_ratio, 2),
        "lower_wick_ratio": round(lower_wick_ratio, 2),
    }

    if body_ratio > max_body_to_range:
        diag["reason"] = "body_too_big"
        return False, diag

    if side == "buy":
        if rsi > oversold:
            diag["reason"] = "rsi_not_oversold"
            return False, diag
        if lower_wick_ratio < min_wick_to_body:
            diag["reason"] = "no_lower_wick_rejection"
            return False, diag
        diag["reason"] = "ok_oversold_lower_wick"
        return True, diag

    if side == "sell":
        if rsi < overbought:
            diag["reason"] = "rsi_not_overbought"
            return False, diag
        if upper_wick_ratio < min_wick_to_body:
            diag["reason"] = "no_upper_wick_rejection"
            return False, diag
        diag["reason"] = "ok_overbought_upper_wick"
        return True, diag

    diag["reason"] = "unknown_side"
    return False, diag
