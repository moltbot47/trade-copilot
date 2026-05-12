"""SL/TP curve verifier — does the tp_scaler curve actually work?

Walks real bars forward, generates one entry every ENTRY_EVERY bars on
each instrument, applies the bot's tp_scaler.compute_tp_sl to set SL/TP
(uses the LATEST forecast confidence from a simple drift estimator), and
forward-simulates intrabar to see whether TP, SL, or the time-out fires
first.

Usage
-----
    python scripts/verify_tp_sl.py
    python scripts/verify_tp_sl.py --interval 5m --bars 1500
    python scripts/verify_tp_sl.py --target-rr 1.0   # override appetite

Aggregates per-instrument:
  - win rate (TP-first hit fraction)
  - average R-multiple (positive = profit)
  - time-to-TP / time-to-SL (median bars held)
  - MAE (max adverse excursion in R) and MFE (max favorable excursion)

This is NOT a strategy backtest — entries are synthetic-spaced so we can
isolate the SL/TP geometry from signal quality. The goal is to confirm
the curve `0.3σ→1.5% / 1σ→3% / 1.5σ→6%...` lands TPs vs gets stopped
out on noise. If win rate is below 30% and MAE > 0.9R the curve is
probably too tight or the appetite R:R is wrong.
"""
from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path

# Repo root → import app.* without installing the package.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np
import pandas as pd

from app.strategies.momentum import compute_atr
from app.strategies.tp_scaler import compute_tp_sl, confidence_to_tp_pct


INSTRUMENTS: list[tuple[str, str]] = [
    ("ES=F", "SP500"),
    ("NQ=F", "NAS100"),
    ("EURUSD=X", "EURUSD"),
    ("GBPUSD=X", "GBPUSD"),
    ("GC=F", "XAUUSD"),
    ("BTC-USD", "BTCUSD"),
    ("ETH-USD", "ETHUSD"),
]


@dataclass
class Trade:
    symbol: str
    side: str
    entry_idx: int
    entry_price: float
    sl: float
    tp: float
    confidence: float
    tp_pct: float
    rr_target: float
    # Outcome (filled after walk-forward)
    exit_idx: int | None = None
    exit_price: float | None = None
    outcome: str = "open"   # "tp" | "sl" | "timeout"
    r_multiple: float = 0.0
    mae_r: float = 0.0      # most adverse excursion in R-units
    mfe_r: float = 0.0      # most favorable
    bars_held: int = 0


def fetch_closes(ticker: str, interval: str, count: int) -> pd.DataFrame | None:
    import yfinance as yf
    period = {"1m": "7d", "5m": "60d", "15m": "60d", "1h": "730d"}.get(interval, "60d")
    df = yf.download(
        ticker, period=period, interval=interval, progress=False, auto_adjust=False,
    )
    if df is None or df.empty:
        return None
    # Yahoo sometimes returns multi-index columns when downloading single tickers.
    if hasattr(df.columns, "nlevels") and df.columns.nlevels > 1:
        df.columns = df.columns.get_level_values(0)
    cols_lower = {c.lower(): c for c in df.columns}
    keep = {}
    for want in ("open", "high", "low", "close"):
        if want in cols_lower:
            keep[want] = df[cols_lower[want]]
    if "close" not in keep or len(keep) < 4:
        return None
    out = pd.DataFrame(keep).dropna().tail(count)
    return out if len(out) >= 60 else None


def estimate_confidence(closes: pd.Series, atr: float, lookback: int = 16) -> tuple[float, float]:
    """Simple drift estimator — same shape as LaT-PFN output, no model.
    Returns (drift_in_atr, confidence_sigma)."""
    ctx = closes.tail(lookback).to_numpy()
    if len(ctx) < 4:
        return 0.0, 0.0
    x = np.arange(len(ctx), dtype=float)
    slope, intercept = np.polyfit(x, ctx, 1)
    # Projected horizon = 12 bars from end of context
    projected = intercept + slope * (len(ctx) - 1 + 12)
    current = float(ctx[-1])
    drift = (projected - current) / atr if atr > 0 else 0.0
    # Forecast std proxy: residual stddev / ATR (per LaT-PFN's σ_ATR convention)
    residuals = ctx - (intercept + slope * x)
    sigma = float(np.std(residuals)) / atr if atr > 0 else 1e-6
    sigma = max(sigma, 1e-6)
    return float(drift), abs(drift) / sigma


def walk_forward(
    df: pd.DataFrame, symbol: str, target_rr: float,
    entry_every: int = 30, max_hold_bars: int = 60,
) -> list[Trade]:
    """Generate entries at regular intervals, walk forward, record outcomes."""
    trades: list[Trade] = []
    closes = df["close"]
    for i in range(40, len(df) - max_hold_bars, entry_every):
        slice_bars = df.iloc[: i + 1]
        atr = compute_atr(slice_bars, 14)
        if not atr or atr <= 0:
            continue
        drift, conf = estimate_confidence(slice_bars["close"], atr)
        if conf < 0.3:
            continue  # below the scaler's floor — skip
        side = "buy" if drift > 0 else "sell"
        current = float(closes.iloc[i])
        lvl = compute_tp_sl(
            side=side, current_price=current, atr=atr,
            confidence=conf, target_rr=target_rr,
        )
        t = Trade(
            symbol=symbol, side=side, entry_idx=i, entry_price=current,
            sl=lvl.stop_loss, tp=lvl.take_profit,
            confidence=conf, tp_pct=lvl.tp_pct, rr_target=target_rr,
        )
        # Walk forward bar by bar — intrabar OHLC for hit detection
        sl_dist = abs(current - lvl.stop_loss)
        for j in range(i + 1, min(i + 1 + max_hold_bars, len(df))):
            hi = float(df["high"].iloc[j])
            lo = float(df["low"].iloc[j])
            # Update MAE / MFE (in R-units)
            if side == "buy":
                adverse = (current - lo) / sl_dist if sl_dist > 0 else 0
                favorable = (hi - current) / sl_dist if sl_dist > 0 else 0
            else:
                adverse = (hi - current) / sl_dist if sl_dist > 0 else 0
                favorable = (current - lo) / sl_dist if sl_dist > 0 else 0
            t.mae_r = max(t.mae_r, adverse)
            t.mfe_r = max(t.mfe_r, favorable)
            # Hit detection (assume SL fires first if both touch same bar — pessimistic)
            if side == "buy":
                if lo <= lvl.stop_loss:
                    t.outcome, t.exit_price, t.exit_idx = "sl", lvl.stop_loss, j
                    t.r_multiple = -1.0
                    break
                if hi >= lvl.take_profit:
                    t.outcome, t.exit_price, t.exit_idx = "tp", lvl.take_profit, j
                    t.r_multiple = (lvl.take_profit - current) / sl_dist if sl_dist > 0 else 0
                    break
            else:
                if hi >= lvl.stop_loss:
                    t.outcome, t.exit_price, t.exit_idx = "sl", lvl.stop_loss, j
                    t.r_multiple = -1.0
                    break
                if lo <= lvl.take_profit:
                    t.outcome, t.exit_price, t.exit_idx = "tp", lvl.take_profit, j
                    t.r_multiple = (current - lvl.take_profit) / sl_dist if sl_dist > 0 else 0
                    break
        else:
            # Timed out
            close_at = float(closes.iloc[min(i + max_hold_bars, len(df) - 1)])
            t.outcome = "timeout"
            t.exit_price = close_at
            t.exit_idx = min(i + max_hold_bars, len(df) - 1)
            if side == "buy":
                t.r_multiple = (close_at - current) / sl_dist if sl_dist > 0 else 0
            else:
                t.r_multiple = (current - close_at) / sl_dist if sl_dist > 0 else 0
        t.bars_held = (t.exit_idx or i) - i
        trades.append(t)
    return trades


def summarize(trades: list[Trade]) -> dict:
    if not trades:
        return {"n": 0}
    wins = [t for t in trades if t.outcome == "tp"]
    losses = [t for t in trades if t.outcome == "sl"]
    timeouts = [t for t in trades if t.outcome == "timeout"]
    avg_r = np.mean([t.r_multiple for t in trades])
    win_r = np.mean([t.r_multiple for t in wins]) if wins else 0.0
    loss_r = np.mean([t.r_multiple for t in losses]) if losses else 0.0
    median_hold_tp = int(np.median([t.bars_held for t in wins])) if wins else 0
    median_hold_sl = int(np.median([t.bars_held for t in losses])) if losses else 0
    mae = np.mean([t.mae_r for t in trades])
    mfe = np.mean([t.mfe_r for t in trades])
    return {
        "n": len(trades),
        "wins": len(wins),
        "losses": len(losses),
        "timeouts": len(timeouts),
        "win_rate": len(wins) / len(trades),
        "avg_r": float(avg_r),
        "avg_win_r": float(win_r),
        "avg_loss_r": float(loss_r),
        "median_bars_to_tp": median_hold_tp,
        "median_bars_to_sl": median_hold_sl,
        "avg_mae_r": float(mae),
        "avg_mfe_r": float(mfe),
        "expectancy_r": float(avg_r),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--interval", default="5m", choices=["1m", "5m", "15m", "1h"])
    ap.add_argument("--bars", type=int, default=1000)
    ap.add_argument("--target-rr", type=float, default=1.5,
                    help="R:R passed to tp_scaler. 1.0=aggressive, 1.5=balanced, 2.0=conservative.")
    ap.add_argument("--entry-every", type=int, default=30)
    ap.add_argument("--max-hold", type=int, default=60)
    args = ap.parse_args()

    print(f"SL/TP verifier · interval={args.interval} · bars={args.bars} · "
          f"target_rr={args.target_rr} · entry_every={args.entry_every} bars")
    print(f"Curve: 0.3σ→{confidence_to_tp_pct(0.3):.1f}% / 1σ→{confidence_to_tp_pct(1):.1f}% "
          f"/ 1.5σ→{confidence_to_tp_pct(1.5):.1f}% / 2.5σ→{confidence_to_tp_pct(2.5):.1f}% / "
          f"max→{confidence_to_tp_pct(4):.1f}%")
    print()

    all_trades: list[Trade] = []
    print(f"{'SYMBOL':<10}{'N':>4}{'WIN%':>7}{'avg R':>8}{'win R':>8}{'loss R':>8}"
          f"{'~TP-bars':>10}{'~SL-bars':>10}{'MAE':>7}{'MFE':>7}")
    print("-" * 90)
    for ticker, label in INSTRUMENTS:
        df = fetch_closes(ticker, args.interval, args.bars)
        if df is None:
            print(f"{label:<10} (no data)")
            continue
        df = df.rename(columns={"close": "close", "open": "open", "high": "high", "low": "low"})
        df.columns = [c.lower() for c in df.columns]
        trades = walk_forward(
            df, label, args.target_rr, args.entry_every, args.max_hold,
        )
        all_trades.extend(trades)
        s = summarize(trades)
        if s["n"] == 0:
            print(f"{label:<10}    0  (no qualifying entries)")
            continue
        print(f"{label:<10}{s['n']:>4}{s['win_rate']*100:>6.1f}%{s['avg_r']:>+8.2f}"
              f"{s['avg_win_r']:>+8.2f}{s['avg_loss_r']:>+8.2f}"
              f"{s['median_bars_to_tp']:>10}{s['median_bars_to_sl']:>10}"
              f"{s['avg_mae_r']:>+7.2f}{s['avg_mfe_r']:>+7.2f}")

    print()
    overall = summarize(all_trades)
    print("=" * 90)
    print(f"OVERALL · {overall['n']} trades · win rate {overall['win_rate']*100:.1f}% · "
          f"expectancy {overall['avg_r']:+.3f}R per trade")
    if overall["n"]:
        print(f"  wins: {overall['wins']}  losses: {overall['losses']}  "
              f"timeouts: {overall['timeouts']}")
        print(f"  avg win {overall['avg_win_r']:+.2f}R · "
              f"avg loss {overall['avg_loss_r']:+.2f}R · "
              f"expectancy {overall['expectancy_r']:+.3f}R")
        print(f"  MAE avg {overall['avg_mae_r']:+.2f}R · MFE avg {overall['avg_mfe_r']:+.2f}R")
        # Diagnostic guidance
        if overall["win_rate"] < 0.30:
            print("  ⚠ Win rate < 30% — TP may be too far or SL too tight; consider lower TP%.")
        if overall["avg_mae_r"] > 0.85:
            print("  ⚠ Average MAE > 0.85R — many trades get close to SL before recovering. "
                  "Consider wider SL (lower target_rr).")
        if overall["avg_mfe_r"] > overall["win_rate"] + 0.5 and overall["win_rate"] < 0.5:
            print("  ⚠ MFE high but win rate low — TP being missed by a hair; "
                  "consider tighter TP (lower target_rr).")
        if overall["expectancy_r"] > 0.05:
            print("  ✓ Positive expectancy — the curve makes money on this sample.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
