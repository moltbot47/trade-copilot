"""Backtest CLI — replay historical OHLCV through a strategy.

Usage
-----
    python -m app.backtest --bot latpfn-quant --symbol BTCUSD \\
        --from 2024-01-01 --to 2024-12-31 --lot 0.01 --spread 2600

CSV data convention
-------------------
The loader looks for a file at::

    backend/backtest_data/<symbol>_<timeframe>.csv

Expected columns (case-insensitive): ts, open, high, low, close, volume.
`ts` may be ISO-8601, RFC3339, or a unix epoch (seconds or millis —
auto-detected by magnitude). Volume is optional. If no file is found,
the engine falls back to a synthetic random-walk so the harness itself
can be smoke-tested.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

from app.backtest.engine import BacktestEngine, _NullForecastClient
from app.strategies.quant_strategy import LatPFNQuantStrategy
from app.strategies.momentum import LatPFNMomentumStrategy

logger = logging.getLogger(__name__)


BACKEND_ROOT = Path(__file__).resolve().parent.parent.parent
DATA_DIR = BACKEND_ROOT / "backtest_data"
RESULTS_DIR = BACKEND_ROOT / "backtest_results"


def _load_csv(
    symbol: str, timeframe: str, start: Optional[str], end: Optional[str]
) -> Optional[pd.DataFrame]:
    """Try a couple of natural file-naming conventions, return None if
    nothing matches."""
    candidates = [
        DATA_DIR / f"{symbol}_{timeframe}.csv",
        DATA_DIR / f"{symbol.lower()}_{timeframe}.csv",
        DATA_DIR / f"{symbol}.csv",
    ]
    path = next((p for p in candidates if p.exists()), None)
    if path is None:
        return None
    df = pd.read_csv(path)
    # Normalize column names to lowercase
    df.columns = [c.lower() for c in df.columns]
    required = {"ts", "open", "high", "low", "close"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(
            f"CSV {path} missing columns: {missing}. "
            f"Required: ts, open, high, low, close (volume optional)."
        )
    # Parse ts. Handle three flavors: ISO strings, epoch seconds, epoch ms.
    if pd.api.types.is_numeric_dtype(df["ts"]):
        # Heuristic: > 10^11 is ms-since-epoch, else seconds
        if df["ts"].iloc[0] > 1e11:
            df["ts"] = pd.to_datetime(df["ts"], unit="ms")
        else:
            df["ts"] = pd.to_datetime(df["ts"], unit="s")
    else:
        df["ts"] = pd.to_datetime(df["ts"])
    if start:
        df = df[df["ts"] >= pd.to_datetime(start)]
    if end:
        df = df[df["ts"] <= pd.to_datetime(end)]
    df = df.sort_values("ts").reset_index(drop=True)
    df = df.set_index("ts")
    return df


def _synthetic_random_walk(
    n: int = 500, start_price: float = 80000.0, seed: int = 42
) -> pd.DataFrame:
    """Sanity-test fallback. Produces a non-trending random walk so the
    engine can prove it runs without falling over."""
    rng = np.random.default_rng(seed)
    rets = rng.normal(0.0, 0.002, size=n)
    closes = start_price * np.exp(np.cumsum(rets))
    highs = closes * (1 + np.abs(rng.normal(0, 0.0005, n)))
    lows = closes * (1 - np.abs(rng.normal(0, 0.0005, n)))
    opens = np.concatenate(([start_price], closes[:-1]))
    vols = rng.integers(100, 1000, size=n).astype(float)
    idx = pd.date_range("2024-01-01", periods=n, freq="1min")
    return pd.DataFrame(
        {"open": opens, "high": highs, "low": lows, "close": closes, "volume": vols},
        index=idx,
    )


def _build_strategy(bot_slug: str, symbol: str, base_qty: float):
    """Instantiate the strategy that matches `bot_slug`.

    The CLI uses a `_NullForecastClient` for the LaT-PFN strategies so
    the harness doesn't require a running model server. Override via
    --drift / --sigma flags for deterministic scenarios.
    """
    client = _NullForecastClient(drift_atr=0.5, sigma_atr=0.1)
    if bot_slug == "latpfn-quant":
        return LatPFNQuantStrategy(
            bot_id=0,
            timeframe="1m",
            latpfn_client=client,
            base_qty=base_qty,
        )
    if bot_slug == "latpfn-momentum":
        return LatPFNMomentumStrategy(
            bot_id=0,
            timeframe="1m",
            latpfn_client=client,
            base_qty=base_qty,
        )
    raise SystemExit(
        f"unknown bot slug: {bot_slug!r}. "
        f"Supported: latpfn-quant, latpfn-momentum"
    )


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m app.backtest",
        description="Walk-forward backtest for Trade Copilot strategies.",
    )
    parser.add_argument(
        "--bot",
        default="latpfn-quant",
        help="Bot slug to backtest (latpfn-quant | latpfn-momentum)",
    )
    parser.add_argument("--symbol", default="BTCUSD")
    parser.add_argument("--timeframe", default="1m")
    parser.add_argument("--from", dest="start", default=None, help="ISO start date")
    parser.add_argument("--to", dest="end", default=None, help="ISO end date")
    parser.add_argument("--lot", type=float, default=0.01)
    parser.add_argument(
        "--spread",
        type=int,
        default=0,
        help="Spread in ticks (1 tick = $0.01 by default; override via --tick-size)",
    )
    parser.add_argument("--tick-size", type=float, default=0.01)
    parser.add_argument(
        "--warmup",
        type=int,
        default=30,
        help="Bars to skip before strategy.on_bar is first called",
    )
    parser.add_argument(
        "--json-out",
        default=None,
        help="Override output JSON path. Default: backtest_results/<run_id>.json",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.WARNING)

    bars = _load_csv(args.symbol, args.timeframe, args.start, args.end)
    if bars is None:
        print(
            f"[backtest] no CSV at backtest_data/{args.symbol}_{args.timeframe}.csv "
            "— using synthetic random-walk fallback (for engine sanity only).",
            file=sys.stderr,
        )
        bars = _synthetic_random_walk()

    strategy = _build_strategy(args.bot, args.symbol, base_qty=args.lot)
    with BacktestEngine(
        strategy=strategy,
        bars=bars,
        instrument=args.symbol,
        lot=args.lot,
        spread_ticks=args.spread,
        tick_size=args.tick_size,
        timeframe=args.timeframe,
        warmup_bars=args.warmup,
    ) as engine:
        result = engine.run()

    # Stdout: markdown report
    print(result.to_markdown())

    # Write JSON for downstream consumers (CI, dashboards).
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    run_id = f"{args.bot}_{args.symbol}_{int(time.time())}"
    out_path = Path(args.json_out) if args.json_out else RESULTS_DIR / f"{run_id}.json"
    with open(out_path, "w") as f:
        json.dump(result.to_dict(), f, indent=2, default=str)
    print(f"\nResults written to: {out_path}", file=sys.stderr)
    return 0


# Allow `python -m app.backtest` to run the CLI.
if __name__ == "__main__":
    raise SystemExit(main())
