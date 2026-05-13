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
        "interval_s": float(os.getenv("DIGEST_INTERVAL_S", "600")),
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

    # Compose a monospace table inside a code block. Discord renders
    # ``` code ``` in a fixed-width font — perfect for column alignment.
    # Columns: SRC SYMBOL SIDE σ R:R $RISK $REW
    header = f"{'SRC':<6}{'SYMBOL':<10}{'SIDE':<5}{'σ':>7}{'R:R':>7}{'$RISK':>9}{'$REW':>9}"
    sep = "─" * len(header)
    lines = [header, sep]
    bull_count = 0
    bear_count = 0
    for r in rows:
        if r.snr > 0:
            bull_count += 1
        else:
            bear_count += 1
        rr_s = f"{r.rr:.2f}" if r.rr is not None else "—"
        risk_s = f"-${r.risk_usd_001:.2f}" if r.risk_usd_001 is not None else "—"
        reward_s = f"+${r.reward_usd_001:.2f}" if r.reward_usd_001 is not None else "—"
        lines.append(
            f"{r.source:<6}{r.symbol:<10}{r.side:<5}"
            f"{r.snr:>+6.2f} {rr_s:>6} {risk_s:>8} {reward_s:>8}"
        )

    body = "```\n" + "\n".join(lines) + "\n```"

    by_source: dict[str, int] = {}
    for r in rows:
        by_source[r.source] = by_source.get(r.source, 0) + 1
    breakdown = ", ".join(f"{n} {s}" for s, n in by_source.items())

    embed = {
        "title": f"📊 Signals digest · {len(rows)} candidates",
        "description": f"`{breakdown}` · last digest window",
        "color": 0x42A5F5,
        "fields": [
            {
                "name": "Candidates",
                "value": body,
                "inline": False,
            },
            {
                "name": "Summary",
                "value": f"🟢 {bull_count} long · 🔴 {bear_count} short",
                "inline": True,
            },
        ],
        "footer": {
            "text": "informational only · runners trade their own configs on their own cadence",
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
