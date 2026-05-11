"""Multi-instrument LaT-PFN confidence scan.

Pulls 240 × 1h closes per instrument from Yahoo Finance (cheap public
source good enough for scan purposes), posts each to the LaT-PFN
forecast service, computes signal-to-noise (drift/σ) at the horizon,
and returns a ranked list.

This is the same logic that ships as /tmp/latpfn_scan.py but lifted
into a reusable module that the Discord bot's /scan command and any
future scheduled scan can share. yfinance is the only added runtime
dep — the runner never depends on it.

We DO NOT use yfinance to drive trading decisions — only the scan.
The actual broker bars come through TradeLocker's data feed.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import urllib.request
from dataclasses import dataclass

logger = logging.getLogger(__name__)


# Default scan basket — fast, balanced across asset classes, fits in
# Discord's 10s interaction deferral window with parallel fetches.
DEFAULT_INSTRUMENTS: list[tuple[str, str]] = [
    ("GC=F", "XAUUSD"),
    ("NQ=F", "NAS100"),
    ("YM=F", "US30"),
    ("ES=F", "SP500"),
    ("GBPJPY=X", "GBPJPY"),
    ("EURUSD=X", "EURUSD"),
    ("BTC-USD", "BTCUSD"),
    ("ETH-USD", "ETHUSD"),
    ("CL=F", "WTI"),
]


def _latpfn_url() -> str | None:
    return (os.getenv("LATPFN_ENDPOINT_URL") or "").strip() or None


@dataclass
class ScanRow:
    label: str
    ticker: str
    current: float
    horizon_mean: float
    drift_pct: float
    horizon_std: float
    snr: float
    direction: str
    inference_ms: float

    @property
    def verdict(self) -> str:
        a = abs(self.snr)
        if a >= 1.5:
            return "STRONG"
        if a >= 0.75:
            return "moderate"
        if a >= 0.3:
            return "weak"
        return "flat"


# ----- Yahoo fetch + forecast — both sync; we wrap in to_thread ----------

def _fetch_closes_sync(ticker: str) -> list[float] | None:
    """Run yfinance's blocking download in a worker thread."""
    try:
        import yfinance as yf  # local import keeps cold-start cheap
        df = yf.download(
            ticker, period="60d", interval="1h",
            progress=False, auto_adjust=False,
        )
        if df is None or df.empty:
            return None
        closes = df["Close"].dropna().tail(240).to_numpy().flatten().tolist()
        if len(closes) < 240:
            return None
        return [float(c) for c in closes]
    except Exception as exc:  # noqa: BLE001 — scan never crashes
        logger.warning("scan: fetch_closes(%s) failed: %s", ticker, exc)
        return None


def _forecast_sync(closes: list[float]) -> dict | None:
    url = _latpfn_url()
    if not url:
        return None
    forecast_url = url.rstrip("/") + "/forecast"
    body = json.dumps({"closes": closes, "n_predict": 12}).encode()
    req = urllib.request.Request(
        forecast_url,
        data=body,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read())
    except Exception as exc:  # noqa: BLE001
        logger.warning("scan: forecast failed: %s", exc)
        return None


async def _scan_one(label: str, ticker: str) -> ScanRow | None:
    """Fetch + forecast for one instrument concurrently."""
    closes = await asyncio.to_thread(_fetch_closes_sync, ticker)
    if not closes:
        return None
    fc = await asyncio.to_thread(_forecast_sync, closes)
    if not fc:
        return None
    try:
        current = float(fc["current_price"])
        mean_path = fc["mean"]
        std_path = fc["std"]
        horizon_mean = float(mean_path[-1])
        horizon_std = float(std_path[-1])
        drift = horizon_mean - current
        drift_pct = drift / current * 100 if current else 0.0
        snr = drift / horizon_std if horizon_std > 0 else 0.0
        direction = "LONG" if drift > 0 else "SHORT"
        return ScanRow(
            label=label,
            ticker=ticker,
            current=current,
            horizon_mean=horizon_mean,
            drift_pct=drift_pct,
            horizon_std=horizon_std,
            snr=snr,
            direction=direction,
            inference_ms=float(fc.get("inference_ms", 0)),
        )
    except (KeyError, TypeError, ValueError) as exc:
        logger.warning("scan: parse failed for %s: %s", label, exc)
        return None


async def run_scan(
    instruments: list[tuple[str, str]] | None = None,
    *,
    timeout_s: float = 25.0,
) -> list[ScanRow]:
    """Run the LaT-PFN scan across `instruments` concurrently.

    Returns rows ranked by |drift/σ| descending (strongest signal first).
    Failures are silently dropped — the scan returns whatever succeeds
    within the timeout budget.
    """
    targets = instruments or DEFAULT_INSTRUMENTS
    if not _latpfn_url():
        logger.info("scan: LATPFN_ENDPOINT_URL not configured; returning empty")
        return []

    started = time.monotonic()
    tasks = [asyncio.create_task(_scan_one(label, ticker)) for ticker, label in targets]
    try:
        done = await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), timeout=timeout_s)
    except asyncio.TimeoutError:
        # Collect whatever's finished — let unfinished tasks GC.
        done = [t.result() if t.done() and not t.exception() else None for t in tasks]

    rows: list[ScanRow] = []
    for r in done:
        if isinstance(r, ScanRow):
            rows.append(r)
    rows.sort(key=lambda r: abs(r.snr), reverse=True)
    elapsed = time.monotonic() - started
    logger.info("scan: produced %d rows in %.2fs", len(rows), elapsed)
    return rows


def format_table(rows: list[ScanRow]) -> str:
    """Render the scan as a monospace block for Discord."""
    if not rows:
        return "_no scan results — LATPFN_ENDPOINT_URL may not be set or all fetches failed_"
    lines = [
        "```",
        f"{'Instrument':14s} {'Dir':6s} {'Δ%':>8s} {'snr':>7s}  verdict",
        "-" * 50,
    ]
    for r in rows:
        lines.append(
            f"{r.label:14s} {r.direction:6s} {r.drift_pct:+7.3f}% {r.snr:+6.2f}  {r.verdict}"
        )
    lines.append("```")
    return "\n".join(lines)
