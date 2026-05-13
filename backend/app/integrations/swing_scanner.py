"""Swing-trade analysis scanner — 15-30 minute hold candidates.

Sibling of :mod:`app.integrations.opportunity_scanner`. Where that scanner
runs on 1-hour bars (12-hour forecast horizon, daily-swing), this one
runs on **15-minute bars** with the same 12-bar forecast → roughly a
**3-hour outer window with typical 15-30 minute resolution time**.

Posts a separate Discord embed style ("SWING SCAN") so the channel feed
can tell scalper / swing / daily-swing apart at a glance.

Each candidate's embed includes:
  - side, symbol, confidence (σ), drift (%)
  - entry, est. TP, est. SL (derived from forecast + ATR-style band)
  - R:R
  - $ risk and $ reward at 0.01 lot (from the contract-size table)
  - est. hold window

Configuration (env)
-------------------
- ``SWING_SCAN_INTERVAL_S``   default 300 (5 minutes)
- ``SWING_SCAN_THRESHOLD``    default 1.0 (σ — slightly looser than the
                              daily scanner since 15m noise is bigger)
- ``SWING_SCAN_COOLDOWN_S``   default 1200 (20 min per-symbol)
- ``SWING_SCAN_ENABLED``      "1" to opt-in; default "0" (disabled)
- ``DISCORD_WEBHOOK_URL``     reused — same channel as other alerts

The scanner shares the contract-size table from discord_signals so the
dollar-P&L math stays consistent across all bot/scanner posts.
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
        "interval_s": float(os.getenv("SWING_SCAN_INTERVAL_S", "300")),
        "threshold": float(os.getenv("SWING_SCAN_THRESHOLD", "1.0")),
        "cooldown_s": float(os.getenv("SWING_SCAN_COOLDOWN_S", "1200")),
        "enabled": os.getenv("SWING_SCAN_ENABLED", "0") == "1",
    }


def _compute_swing_levels(
    current: float,
    horizon_mean: float,
    horizon_std: float,
    direction: str,
) -> tuple[float, float, float] | None:
    """Derive entry/TP/SL for a 15m swing candidate.

    Uses the forecast mean as the target (TP), with the SL placed 1×σ
    against the entry. This is the same shape as the runner uses on 1m
    bars but with the bigger σ that 15m volatility implies → wider stops
    naturally accommodate the longer hold.

    Returns None if the forecast is too flat to set meaningful levels.
    """
    if horizon_std <= 0:
        return None
    # Side-aware: TP = forecast mean, SL = entry minus σ (for buy)
    if direction == "LONG":
        tp = horizon_mean
        sl = current - horizon_std
        if tp <= current or sl >= current:
            return None
    else:  # SHORT
        tp = horizon_mean
        sl = current + horizon_std
        if tp >= current or sl <= current:
            return None
    return float(current), float(tp), float(sl)


def _confidence_label(snr: float) -> str:
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
    if abs(snr) < threshold:
        return False
    entry = _cooldown.get(symbol)
    now = time.monotonic()
    if entry is None:
        return True
    if entry.last_direction != direction:
        return True  # direction flip → always interesting
    return (now - entry.last_alert_ts) >= cooldown_s


def _mark_alerted(symbol: str, direction: str) -> None:
    _cooldown[symbol] = _CooldownEntry(last_alert_ts=time.monotonic(), last_direction=direction)


async def _post_swing_candidate(row: Any) -> None:
    """Post one swing candidate to Discord. Includes $ risk/reward + est. hold."""
    from app.integrations.discord_signals import (
        _CONTRACT_SIZE_PER_LOT,
        _webhook_url,
    )

    url = _webhook_url()
    if not url:
        return

    is_buy = row.direction == "LONG"
    color = 0x4CAF50 if is_buy else 0xE53935
    emoji = "🟢" if is_buy else "🔴"
    side_label = "BUY" if is_buy else "SELL"

    levels = _compute_swing_levels(
        row.current, row.horizon_mean, row.horizon_std, row.direction
    )
    if levels is None:
        # Forecast too flat — skip rather than post a noise embed.
        return
    entry_price, tp_price, sl_price = levels

    fields: list[dict[str, Any]] = [
        {
            "name": "Setup",
            "value": f"{emoji} **{side_label}** {row.label} @ `{entry_price:,.4f}`",
            "inline": False,
        },
        {
            "name": "Take Profit",
            "value": f"`{tp_price:,.4f}` ({((tp_price-entry_price)/entry_price*100):+.2f}%)",
            "inline": True,
        },
        {
            "name": "Stop Loss",
            "value": f"`{sl_price:,.4f}` ({((sl_price-entry_price)/entry_price*100):+.2f}%)",
            "inline": True,
        },
    ]

    # R:R + $ risk/reward at 0.01 lot
    tp_dist = abs(tp_price - entry_price)
    sl_dist = abs(entry_price - sl_price)
    if sl_dist > 0:
        rr = tp_dist / sl_dist
        fields.append({"name": "R:R", "value": f"{rr:.2f} : 1", "inline": True})
        contract = _CONTRACT_SIZE_PER_LOT.get(row.label.upper())
        if contract is not None:
            risk_usd = sl_dist * contract * 0.01
            reward_usd = tp_dist * contract * 0.01
            fields.append({
                "name": "Risk @ 0.01 lot",
                "value": f"-${risk_usd:.2f}",
                "inline": True,
            })
            fields.append({
                "name": "Reward @ 0.01 lot",
                "value": f"+${reward_usd:.2f}",
                "inline": True,
            })

    fields.append({
        "name": "Confidence",
        "value": f"{row.snr:+.2f}σ",
        "inline": True,
    })
    fields.append({
        "name": "Forecast move",
        "value": f"{row.drift_pct:+.2f}%",
        "inline": True,
    })
    fields.append({
        "name": "Est. hold",
        "value": "15–30 min (3h outer)",
        "inline": True,
    })

    confidence_label = _confidence_label(row.snr)

    embed = {
        "title": f"🌀 SWING · {emoji} {side_label} {row.label}",
        "description": f"`{confidence_label}` · 15m bars · 3-hour forecast horizon",
        "color": color,
        "fields": fields,
        "footer": {
            "text": (
                "informational only · longer-hold candidates · not auto-traded "
                "by the runner — manual reference"
            ),
        },
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

    try:
        async with httpx.AsyncClient(timeout=5.0) as c:
            r = await c.post(url, json={"embeds": [embed]})
            if r.status_code >= 400:
                logger.warning(
                    "swing_scanner post failed (%s): %s",
                    r.status_code, r.text[:240],
                )
    except Exception as exc:  # noqa: BLE001 — never crash the scanner
        logger.warning("swing_scanner post raised: %s", exc)


async def _one_pass() -> int:
    """Run one 15m scan + post candidates. Returns the number of alerts posted."""
    from app.integrations.latpfn_scan import EXPANDED_INSTRUMENTS, run_scan

    cfg = _config()
    rows = await run_scan(EXPANDED_INSTRUMENTS, timeout_s=90.0, interval="15m")
    posted = 0
    for r in rows:
        if _should_alert(
            r.label, r.snr, r.direction,
            threshold=cfg["threshold"], cooldown_s=cfg["cooldown_s"],
        ):
            await _post_swing_candidate(r)
            _mark_alerted(r.label, r.direction)
            posted += 1
    logger.info(
        "swing_scanner: %d/%d above %.2fσ, %d swing candidates posted",
        sum(1 for r in rows if abs(r.snr) >= cfg["threshold"]),
        len(rows),
        cfg["threshold"],
        posted,
    )
    return posted


async def swing_scanner_task() -> None:
    """Long-lived asyncio task: scan every SWING_SCAN_INTERVAL_S seconds."""
    cfg = _config()
    if not cfg["enabled"]:
        logger.info("swing_scanner: SWING_SCAN_ENABLED=0; task is a no-op")
        while True:
            try:
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                return

    logger.info(
        "swing_scanner: starting (interval=%ds, threshold=%.2fσ, cooldown=%ds)",
        int(cfg["interval_s"]), cfg["threshold"], int(cfg["cooldown_s"]),
    )

    # Offset from the daily scanner so they don't both hit LaT-PFN at the
    # same instant. Daily starts at +30s; we start at +90s.
    try:
        await asyncio.sleep(90)
    except asyncio.CancelledError:
        return

    while True:
        try:
            await _one_pass()
        except Exception as exc:  # noqa: BLE001
            logger.warning("swing_scanner pass raised: %s", exc)
        try:
            await asyncio.sleep(cfg["interval_s"])
        except asyncio.CancelledError:
            return
