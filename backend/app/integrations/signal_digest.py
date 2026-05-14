"""Signal digest — batches scanner outputs into ONE Discord post per window.

Why
---
Before: opportunity_scanner + swing_scanner each posted per-symbol embeds
as they fired. On a busy 5-minute scan, that's 4-8 Discord pings per
cycle — noisy enough that the operator asked us to "just send one
report" instead.

After: both scanners enqueue rows into this module; a separate flusher
task posts one combined embed every DIGEST_INTERVAL_S seconds, listing
every signal from the window with its source bot tag, σ, side, R:R,
and $ risk/reward at 0.01 lot.

Config
------
- ``DIGEST_INTERVAL_S``     default 600 (10 min)
- ``DIGEST_ENABLED``        "1" to opt-in; default "1" (on — operator
                            asked for this; keeping default off would
                            be a footgun)
- ``DISCORD_WEBHOOK_URL``   reused

Trade-execution signals from the strategy runner are NOT routed here —
those are high-signal trade-placed alerts and still post individually.
"""
from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import httpx

logger = logging.getLogger(__name__)


@dataclass
class DigestRow:
    """One signal collected in the current digest window."""
    source: str          # "DAILY" or "SWING" (or anything new we add)
    symbol: str
    side: str            # "BUY" / "SELL"
    snr: float           # confidence in σ (signed: positive = bullish)
    drift_pct: float | None = None
    entry: float | None = None
    tp: float | None = None
    sl: float | None = None
    rr: float | None = None
    risk_usd_001: float | None = None
    reward_usd_001: float | None = None
    enqueued_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


_queue: list[DigestRow] = []
_queue_lock = asyncio.Lock()


def _config() -> dict[str, Any]:
    return {
        # Default 1800 = 30 min. Operator requested less-frequent posts
        # 2026-05-14 (was 600 = 10 min). Lower frequency reduces
        # notification fatigue; signals stay actionable within a 30-min
        # window for most timeframes.
        "interval_s": float(os.getenv("DIGEST_INTERVAL_S", "1800")),
        "enabled": os.getenv("DIGEST_ENABLED", "1") == "1",
    }


async def enqueue_signal(row: DigestRow) -> None:
    """Add a signal to the digest queue.

    No-op (silent) if DIGEST_ENABLED=0 — caller should fall back to its
    own direct-post path in that case.
    """
    if not _config()["enabled"]:
        return
    async with _queue_lock:
        _queue.append(row)


def is_enabled() -> bool:
    """Public helper for scanners that need to decide between digest
    enqueue and direct post."""
    return _config()["enabled"]


async def _flush_now() -> int:
    """Post all queued signals as a single Discord embed, then clear.

    Returns the number of signals flushed. Best-effort — failures are
    logged but the queue is still cleared so we don't double-post on
    the next run.
    """
    from app.integrations.discord_signals import _webhook_url

    async with _queue_lock:
        if not _queue:
            return 0
        rows = list(_queue)
        _queue.clear()

    url = _webhook_url()
    if not url:
        logger.info("digest: %d signals dropped (no webhook configured)", len(rows))
        return 0

    # Per-signal embed fields with full entry/SL/TP — matches the scan-
    # report format. Discord embeds cap at 25 fields and 6000 chars, so
    # we cap at 15 signals per digest. Sort by abs(snr) desc so strongest
    # signals show first.
    rows_sorted = sorted(rows, key=lambda x: abs(x.snr), reverse=True)[:15]
    bull_count = sum(1 for r in rows if r.snr > 0)
    bear_count = sum(1 for r in rows if r.snr < 0)

    fields: list[dict[str, Any]] = []
    for r in rows_sorted:
        emoji = "🟢" if r.snr > 0 else "🔴"
        side = r.side.upper()
        sigma_abs = abs(r.snr)

        # Build the value block conditionally — DAILY rows may have
        # entry/sl/tp populated now (post-2026-05-14 update); rows that
        # still don't have them fall back to a compact summary.
        value_lines = []
        if r.entry is not None:
            value_lines.append(f"**Entry** `{r.entry:.5f}`")
        if r.sl is not None and r.entry is not None:
            sl_pct = (r.sl - r.entry) / r.entry * 100
            sl_dollar = f" · risk **-${r.risk_usd_001:.2f}**" if r.risk_usd_001 is not None else ""
            value_lines.append(f"**SL** `{r.sl:.5f}` ({sl_pct:+.2f}%){sl_dollar}")
        if r.tp is not None and r.entry is not None:
            tp_pct = (r.tp - r.entry) / r.entry * 100
            tp_dollar = f" · reward **+${r.reward_usd_001:.2f}**" if r.reward_usd_001 is not None else ""
            value_lines.append(f"**TP** `{r.tp:.5f}` ({tp_pct:+.2f}%){tp_dollar}")
        rr_str = f"{r.rr:.2f}:1" if r.rr is not None else "—"
        drift_str = f"drift {r.drift_pct:+.2f}%" if r.drift_pct is not None else ""
        value_lines.append(f"R:R **{rr_str}** · {drift_str} · src `{r.source}`")
        value = "\n".join(value_lines)

        fields.append({
            "name": f"{emoji} {r.symbol} {side} · {sigma_abs:.2f}σ",
            "value": value,
            "inline": False,
        })

    by_source: dict[str, int] = {}
    for r in rows:
        by_source[r.source] = by_source.get(r.source, 0) + 1
    breakdown = ", ".join(f"{n} {s}" for s, n in by_source.items())
    overflow_note = f" · top 15 of {len(rows)}" if len(rows) > 15 else ""

    embed = {
        "title": f"📊 Signals digest · {len(rows)} candidates",
        "description": f"`{breakdown}`{overflow_note} · 🟢 {bull_count} long · 🔴 {bear_count} short",
        "color": 0x42A5F5,
        "fields": fields,
        "footer": {
            "text": "informational · entry/SL/TP at 0.01 lot · runners trade their own configs",
        },
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

    try:
        async with httpx.AsyncClient(timeout=8.0) as c:
            r = await c.post(url, json={"embeds": [embed]})
            if r.status_code >= 400:
                logger.warning("digest post failed (%s): %s", r.status_code, r.text[:240])
                return 0
    except Exception as exc:  # noqa: BLE001
        logger.warning("digest post raised: %s", exc)
        return 0

    logger.info("digest flushed: %d signals (%s)", len(rows), breakdown)
    return len(rows)


async def digest_flusher_task() -> None:
    """Periodic flusher. Posts once per DIGEST_INTERVAL_S if queue non-empty."""
    cfg = _config()
    if not cfg["enabled"]:
        logger.info("signal_digest: DIGEST_ENABLED=0; task is a no-op")
        while True:
            try:
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                return

    logger.info("signal_digest: starting (interval=%ds)", int(cfg["interval_s"]))

    # Initial offset so we don't fire mid-deploy.
    try:
        await asyncio.sleep(60)
    except asyncio.CancelledError:
        return

    while True:
        try:
            await _flush_now()
        except Exception as exc:  # noqa: BLE001
            logger.warning("digest flush raised: %s", exc)
        try:
            await asyncio.sleep(cfg["interval_s"])
        except asyncio.CancelledError:
            return
