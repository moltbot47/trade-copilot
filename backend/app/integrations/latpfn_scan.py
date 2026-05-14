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


# Default scan basket — balanced across asset classes. Used by the /scan
# Discord command and (smaller subset) the opportunity_scanner task.
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


# Expanded basket for the 5-min opportunity_scanner — covers indices,
# metals, FX majors + crosses, crypto, and oil. 24 instruments. Each one
# is a (yfinance_ticker, display_label) pair. Resource math: 24 × 1.5s
# LaT-PFN inference per scan × 12 scans/hour = ~432 CPU-sec/hour on DO,
# i.e. ~12% of a single vCPU — well within budget.
EXPANDED_INSTRUMENTS: list[tuple[str, str]] = [
    # ---- Equity indices ----
    ("ES=F", "SP500"),
    ("NQ=F", "NAS100"),
    ("YM=F", "US30"),
    ("RTY=F", "US2000"),
    ("^GDAXI", "DE40"),
    ("^FTSE", "FTSE100"),
    ("^AXJO", "AU200"),
    ("^HSI", "HK50"),
    # ---- Metals + commodities ----
    ("GC=F", "XAUUSD"),
    ("SI=F", "XAGUSD"),
    ("CL=F", "WTI"),
    ("NG=F", "NATGAS"),
    # ---- FX majors ----
    ("EURUSD=X", "EURUSD"),
    ("GBPUSD=X", "GBPUSD"),
    ("USDJPY=X", "USDJPY"),
    ("AUDUSD=X", "AUDUSD"),
    ("USDCAD=X", "USDCAD"),
    ("NZDUSD=X", "NZDUSD"),
    ("USDCHF=X", "USDCHF"),
    # ---- FX crosses ----
    ("GBPJPY=X", "GBPJPY"),
    ("EURJPY=X", "EURJPY"),
    ("EURGBP=X", "EURGBP"),
    # ---- Crypto majors ----
    ("BTC-USD", "BTCUSD"),
    ("ETH-USD", "ETHUSD"),
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

_PERIOD_FOR_INTERVAL: dict[str, str] = {
    # yfinance constraints: intraday intervals have shorter max periods.
    # We need ~240 bars; pick the smallest period that yields enough.
    "1m": "7d",
    "5m": "30d",
    "15m": "5d",   # 240 × 15m = 60h ≈ 2.5d; 5d is comfy
    "30m": "10d",
    "1h": "60d",
    "1d": "2y",
}


def _fetch_closes_sync(ticker: str, *, interval: str = "1h") -> list[float] | None:
    """Run yfinance's blocking download in a worker thread.

    interval: yfinance bar size (e.g. "1h" for the daily scanner,
    "15m" for the swing scanner). The matching ``period`` is chosen
    from ``_PERIOD_FOR_INTERVAL`` so we get >= 240 bars without
    hitting yfinance's interval/period constraints.
    """
    try:
        import yfinance as yf  # local import keeps cold-start cheap
        period = _PERIOD_FOR_INTERVAL.get(interval, "60d")
        df = yf.download(
            ticker, period=period, interval=interval,
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


async def _get_system_broker_fetcher():
    """Build a BarFetcher using the first user with a working broker
    connection. Used by scanners to fetch real-time broker bars
    instead of yfinance's delayed feed. Returns None if no user has a
    usable token.

    The "system" user is just whoever's available — we're not trading
    on their account, just reading bars. Token gets auto-refreshed and
    persisted on first 401.
    """
    from sqlalchemy import select
    from app.core.crypto import decrypt, encrypt
    from app.core.tradelocker_client import TradeLockerClient, TradeLockerError
    from app.db.database import SessionLocal
    from app.db.models import User
    from app.strategies.data_feed import BarFetcher

    client = TradeLockerClient(env="live")
    db = SessionLocal()
    try:
        users = db.scalars(select(User).where(User.tradelocker_token.isnot(None))).all()
        for user in users:
            try:
                token = decrypt(user.tradelocker_token)
                # Light touch — just verify the token works before returning the fetcher.
                await client.get_account_state(
                    user.tradelocker_account_id, token, user.tradelocker_acc_num or "1"
                )
                logger.info("scan: using broker bars via user=%s", user.email)
                return BarFetcher(
                    client=client,
                    account_id=user.tradelocker_account_id,
                    token=token,
                    acc_num=user.tradelocker_acc_num or "1",
                )
            except TradeLockerError as e:
                if "401" in str(e) and user.tradelocker_refresh_token:
                    try:
                        rt = decrypt(user.tradelocker_refresh_token)
                        fresh = await client.refresh_access_token(rt)
                        new_token = fresh["access_token"]
                        u = db.get(User, user.id)
                        u.tradelocker_token = encrypt(new_token)
                        db.commit()
                        logger.info("scan: refreshed token for user=%s, using broker bars", user.email)
                        return BarFetcher(
                            client=client,
                            account_id=user.tradelocker_account_id,
                            token=new_token,
                            acc_num=user.tradelocker_acc_num or "1",
                        )
                    except Exception as exc:
                        logger.warning("scan: token refresh failed for %s: %s", user.email, exc)
                        continue
                continue
            except Exception as exc:
                logger.warning("scan: probe failed for %s: %s", user.email, exc)
                continue
    finally:
        db.close()
    logger.warning("scan: no user with working broker connection — falling back to yfinance")
    return None


async def _scan_one(
    label: str, ticker: str, *, interval: str = "1h", fetcher=None
) -> ScanRow | None:
    """Fetch + forecast for one instrument concurrently.

    If `fetcher` is provided (a BarFetcher), bars come from the broker
    in real-time. Otherwise falls back to yfinance (delayed feed).
    """
    if fetcher is not None:
        # Broker bars (real-time)
        try:
            # Request more than 240 so we can trim to exactly 240 after dropna
            bars = await fetcher.fetch_bars(label, timeframe=interval, count=320)
            if bars is None or len(bars) < 240:
                return None
            if bool(getattr(bars, "attrs", {}).get("synthetic")):
                return None
            closes = bars["close"].dropna().tolist()[-240:]
            if len(closes) < 240:
                return None
        except Exception as exc:  # noqa: BLE001
            logger.debug("scan: broker fetch failed for %s: %s", label, exc)
            return None
    else:
        # yfinance fallback (delayed)
        closes = await asyncio.to_thread(_fetch_closes_sync, ticker, interval=interval)
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
    interval: str = "1h",
    use_broker_bars: bool = True,
) -> list[ScanRow]:
    """Run the LaT-PFN scan across `instruments` concurrently.

    interval: bar size. Default "1h" → 12-bar horizon = ~12h.
    use_broker_bars: when True (default 2026-05-14), fetch bars from
        TradeLocker via BarFetcher — real-time, accurate. When False or
        no broker user available, falls back to yfinance (delayed).
        The runner has always used broker bars; bringing scanners in
        line eliminates the stale-price problem that was showing wrong
        entry prices in the digest (GBPUSD 1.35914 vs broker 1.35047).

    Returns rows ranked by |drift/σ| descending (strongest signal first).
    Failures are silently dropped — the scan returns whatever succeeds
    within the timeout budget.
    """
    fetcher = None
    if use_broker_bars:
        try:
            fetcher = await _get_system_broker_fetcher()
        except Exception as exc:
            logger.warning("scan: broker fetcher init failed: %s; falling back to yfinance", exc)
    targets = instruments or DEFAULT_INSTRUMENTS
    if not _latpfn_url():
        logger.info("scan: LATPFN_ENDPOINT_URL not configured; returning empty")
        return []

    started = time.monotonic()
    tasks = [
        asyncio.create_task(_scan_one(label, ticker, interval=interval, fetcher=fetcher))
        for ticker, label in targets
    ]
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
