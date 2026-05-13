"""Continuous LaT-PFN opportunity scanner.

Runs as a background asyncio task in the FastAPI lifespan. Every
``SCAN_INTERVAL_S`` seconds (default 300 = 5 min) it scans the expanded
24-instrument basket via :mod:`app.integrations.latpfn_scan` and posts a
Discord embed for every row whose ``|drift/σ| >= SCAN_THRESHOLD``.

Why this is its own task instead of a cron
------------------------------------------
The market never stops on Sundays for FX/crypto and indices have
overlap-hour windows that matter for compounding. A periodic in-process
task keeps the lookback alive without needing external schedulers.

Per-symbol cooldown
-------------------
A naïve "alert if above threshold" would spam — once SP500 crosses
1.5σ it tends to stay there for hours. We keep an in-memory map of
``last_alert_at[symbol]`` and skip re-alerts unless either:
  1. ``SCAN_COOLDOWN_S`` has elapsed since the last alert
  2. OR the direction flipped (buy ↔ sell) — direction flips are
     always interesting regardless of cooldown

Resource budget (24 instruments × 1.5s × 12 scans/hour)
-------------------------------------------------------
- DO LaT-PFN VM:    ~432 CPU-seconds/hr → ~12% of one vCPU
- DO bandwidth:     ~3 MB/day
- Fly backend RAM:  ~10 MB transient per scan
- Discord posts:    average 0-3 per scan; never close to the 30/min limit

Configuration (env)
-------------------
- ``SCAN_INTERVAL_S``      default 300
- ``SCAN_THRESHOLD``       default 1.5 (σ above noise)
- ``SCAN_COOLDOWN_S``      default 1800 (30 min per-symbol)
- ``SCAN_ENABLED``         "1" to opt-in; default "0" (disabled)
- ``DISCORD_WEBHOOK_URL``  reused — the same channel that gets admin alerts
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import httpx

logger = logging.getLogger(__name__)


@dataclass
class _CooldownEntry:
    last_alert_ts: float
    last_direction: str


_cooldown: dict[str, _CooldownEntry] = {}


def _config() -> dict[str, Any]:
    return {
        "interval_s": float(os.getenv("SCAN_INTERVAL_S", "300")),
        "threshold": float(os.getenv("SCAN_THRESHOLD", "1.5")),
        "cooldown_s": float(os.getenv("SCAN_COOLDOWN_S", "1800")),
        "enabled": os.getenv("SCAN_ENABLED", "0") == "1",
    }


async def _post_opportunity(row: Any) -> None:
    """Post a single LaT-PFN opportunity to the global Discord webhook.

    Embed is intentionally compact: side · symbol · confidence · move%
    so the channel feed stays scannable. Failures log a warning and the
    scanner moves on; one bad alert won't block the next tick.
    """
    from app.integrations.discord_signals import _webhook_url  # reuse global lookup

    url = _webhook_url()
    if not url:
        return  # no webhook configured; silent

    is_buy = row.direction == "LONG"
    color = 0x4CAF50 if is_buy else 0xE53935  # green / red
    emoji = "🟢" if is_buy else "🔴"
    drift_sign = "+" if row.drift_pct >= 0 else ""

    embed = {
        "title": f"🎯 OPPORTUNITY · {emoji} {row.direction} {row.label}",
        "description": (
            f"`{row.confidence_label}` · forecast move {drift_sign}{row.drift_pct:.2f}% "
            f"over the next 12 bars"
        ),
        "color": color,
        "fields": [
            {"name": "Symbol", "value": f"`{row.label}`", "inline": True},
            {"name": "Side", "value": f"**{row.direction}**", "inline": True},
            {"name": "Confidence", "value": f"{row.snr:+.2f}σ", "inline": True},
            {"name": "Spot", "value": f"`{row.current:.4f}`", "inline": True},
            {"name": "Forecast +12h", "value": f"`{row.horizon_mean:.4f}`", "inline": True},
            {"name": "σ", "value": f"`{row.horizon_std:.4f}`", "inline": True},
        ],
        "footer": {
            "text": (
                "informational only · the operator's bots act on their own "
                "schedule · scanner ≠ trade signal"
            ),
        },
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

    try:
        async with httpx.AsyncClient(timeout=5.0) as c:
            r = await c.post(url, json={"embeds": [embed]})
            if r.status_code >= 400:
                logger.warning(
                    "opportunity scanner post failed (%s): %s",
                    r.status_code, r.text[:240],
                )
    except Exception as exc:  # noqa: BLE001 — never crash the scanner
        logger.warning("opportunity scanner post raised: %s", exc)


def _confidence_label(snr: float) -> str:
    """Match the verdict tiers used in the on-demand scan."""
    a = abs(snr)
    if a >= 2.5:
        return "HIGH CONVICTION"
    if a >= 1.5:
        return "STRONG signal"
    if a >= 0.75:
        return "moderate edge"
    if a >= 0.3:
        return "weak drift"
    return "flat"


def _should_alert(symbol: str, snr: float, direction: str, *, threshold: float, cooldown_s: float) -> bool:
    """Apply cooldown + direction-flip override."""
    if abs(snr) < threshold:
        return False
    entry = _cooldown.get(symbol)
    now = time.monotonic()
    if entry is None:
        return True
    # Direction flip always alerts — that's the most interesting signal
    if entry.last_direction != direction:
        return True
    return (now - entry.last_alert_ts) >= cooldown_s


def _mark_alerted(symbol: str, direction: str) -> None:
    _cooldown[symbol] = _CooldownEntry(last_alert_ts=time.monotonic(), last_direction=direction)


async def _one_pass() -> int:
    """Run one scan + emit alerts. Returns the number of alerts emitted.

    Emit path: if signal_digest is enabled (default), each candidate is
    enqueued into the shared digest and the digest_flusher_task posts
    a single combined embed every DIGEST_INTERVAL_S. Otherwise we fall
    back to direct per-symbol posts (legacy behavior).
    """
    from app.integrations.latpfn_scan import EXPANDED_INSTRUMENTS, run_scan
    from app.integrations.signal_digest import DigestRow, enqueue_signal, is_enabled

    cfg = _config()
    rows = await run_scan(EXPANDED_INSTRUMENTS, timeout_s=60.0)
    posted = 0
    digest_on = is_enabled()
    for r in rows:
        r.confidence_label = _confidence_label(r.snr)  # type: ignore[attr-defined]
        if _should_alert(
            r.label, r.snr, r.direction,
            threshold=cfg["threshold"], cooldown_s=cfg["cooldown_s"],
        ):
            if digest_on:
                await enqueue_signal(DigestRow(
                    source="DAILY",
                    symbol=r.label,
                    side="BUY" if r.direction == "LONG" else "SELL",
                    snr=r.snr,
                    drift_pct=r.drift_pct,
                ))
            else:
                await _post_opportunity(r)
            _mark_alerted(r.label, r.direction)
            posted += 1
    logger.info(
        "opportunity_scanner: %d/%d above %.2fσ, %d %s",
        sum(1 for r in rows if abs(r.snr) >= cfg["threshold"]),
        len(rows),
        cfg["threshold"],
        posted,
        "queued in digest" if digest_on else "alerts posted",
    )
    return posted


async def opportunity_scanner_task() -> None:
    """Long-lived asyncio task: scan every SCAN_INTERVAL_S seconds.

    No-op if SCAN_ENABLED != "1". Errors are logged and the loop
    continues — a single bad scan never kills the task.
    """
    cfg = _config()
    if not cfg["enabled"]:
        logger.info("opportunity_scanner: SCAN_ENABLED=0; task is a no-op")
        # Sit idle but stay alive so we don't need to special-case in lifespan.
        while True:
            try:
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                return

    logger.info(
        "opportunity_scanner: starting (interval=%ds, threshold=%.2fσ, cooldown=%ds)",
        int(cfg["interval_s"]), cfg["threshold"], int(cfg["cooldown_s"]),
    )

    # Initial small delay so we don't fire while the rest of the app is
    # still warming up after a deploy.
    try:
        await asyncio.sleep(30)
    except asyncio.CancelledError:
        return

    while True:
        try:
            await _one_pass()
        except Exception as exc:  # noqa: BLE001 — never let the loop die
            logger.warning("opportunity_scanner pass raised: %s", exc)
        try:
            await asyncio.sleep(cfg["interval_s"])
        except asyncio.CancelledError:
            return
