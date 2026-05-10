"""Discord webhook publisher for runner decisions.

Single global webhook (configurable via env: DISCORD_SIGNALS_WEBHOOK_URL).
Per-user DM support is intentionally deferred — to be added once we
introduce Discord OAuth in the user-settings flow.

Posts a rich embed for *interesting* runner decisions only:

  HIGH-SIGNAL (always post):
    entry_buy, entry_sell             — bot opened a new position
    scale_in                          — bot added a leg to an existing cohort
    partial_close                     — bot took 50% off at +1R
    hard_exit                         — bot closed (TP, stop, forecast flip)
    skip_kill_switch                  — daily loss limit halted trading
    skip_exhaustion_filter            — momentum signal blocked by exhaustion
                                        gate (useful: tells you when the bot
                                        SAW a setup but waited)

  ROUTINE (never post):
    manage                            — managing existing cohort, no change
    skip_below_threshold              — confidence too low, normal idle state
    skip_existing_position            — won't pyramid, normal
    skip_position_cap                 — cap reached, normal once 1 open
    error                             — logged separately to Sentry/logs

A single failed Discord post must NEVER break the trading loop. All HTTP
calls are best-effort; we log warnings on failure and move on.
"""
from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

import httpx

logger = logging.getLogger(__name__)

# Two env-var names supported (DISCORD_WEBHOOK_URL is the older one some
# deployments are using; DISCORD_SIGNALS_WEBHOOK_URL is the newer canonical
# name). Either works — the publisher checks both.
_DISCORD_WEBHOOK_ENV_NAMES = ("DISCORD_SIGNALS_WEBHOOK_URL", "DISCORD_WEBHOOK_URL")
DISCORD_WEBHOOK_ENV = _DISCORD_WEBHOOK_ENV_NAMES[0]  # canonical (kept for backwards-compat imports)

# Decision allowlist — these post to Discord. Includes skip_below_threshold
# so every scan with a confidence score lands in the channel — useful for
# watching the bot "think" in real time even when it doesn't fire.
HIGH_SIGNAL_DECISIONS = frozenset(
    {
        "entry_buy",
        "entry_sell",
        "scale_in",
        "partial_close",
        "hard_exit",
        "skip_kill_switch",
        "skip_exhaustion_filter",
        "skip_below_threshold",   # confidence-bearing scan
        "skip_existing_position", # confidence-bearing scan blocked by exposure
        "skip_position_cap",      # confidence-bearing scan blocked by cap
    }
)

# Color palette per decision (Discord embed `color` is decimal RGB).
_COLORS = {
    "entry_buy": 0x00C853,        # green
    "entry_sell": 0xD32F2F,       # red
    "scale_in": 0x00B0FF,         # blue
    "partial_close": 0xFFC107,    # amber
    "hard_exit": 0x9C27B0,        # purple
    "skip_kill_switch": 0xFF1744, # bright red
    "skip_exhaustion_filter": 0x607D8B,  # blue-gray
}

_EMOJIS = {
    "entry_buy": "🟢",
    "entry_sell": "🔴",
    "scale_in": "📈",
    "partial_close": "💰",
    "hard_exit": "🚪",
    "skip_kill_switch": "🛑",
    "skip_exhaustion_filter": "⏸️",
    "skip_below_threshold": "🔍",
    "skip_existing_position": "🔒",
    "skip_position_cap": "🧯",
}

# Skip-decisions get a more muted blue-grey color since they are scans, not trades.
_COLORS["skip_below_threshold"] = 0x546E7A
_COLORS["skip_existing_position"] = 0x546E7A
_COLORS["skip_position_cap"] = 0x546E7A


def _webhook_url() -> str | None:
    """Return the first non-empty webhook URL from the supported env names."""
    for name in _DISCORD_WEBHOOK_ENV_NAMES:
        v = os.environ.get(name)
        if v:
            return v
    return None


async def post_decision(
    decision: str,
    *,
    bot_id: int,
    timeframe: str,
    symbol: str,
    current_price: float | None = None,
    forecast_drift: float | None = None,
    forecast_confidence: float | None = None,
    threshold: float | None = None,
    reason: str | None = None,
    extra: dict[str, Any] | None = None,
) -> None:
    """Post a runner decision to the configured Discord webhook.

    Best-effort — failures are logged at WARNING but not raised. Decisions
    not in the high-signal allowlist are silently skipped.
    """
    if decision not in HIGH_SIGNAL_DECISIONS:
        return
    url = _webhook_url()
    if not url:
        return  # not configured; silent

    extra = extra or {}
    color = _COLORS.get(decision, 0x90A4AE)
    emoji = _EMOJIS.get(decision, "📢")

    fields: list[dict[str, Any]] = []
    if current_price is not None:
        fields.append(
            {"name": "Price", "value": f"${current_price:,.2f}", "inline": True}
        )
    if forecast_confidence is not None:
        fields.append(
            {
                "name": "Confidence",
                "value": f"{forecast_confidence:.2f}σ",
                "inline": True,
            }
        )
    if forecast_drift is not None:
        sign = "+" if forecast_drift >= 0 else ""
        fields.append(
            {
                "name": "Drift",
                "value": f"{sign}{forecast_drift:.2f} ATR",
                "inline": True,
            }
        )
    if threshold is not None:
        fields.append(
            {"name": "Threshold", "value": f"{threshold:.2f}σ", "inline": True}
        )
    if "qty" in extra:
        fields.append({"name": "Lot", "value": f"{extra['qty']:.2f}", "inline": True})
    if "sl" in extra and extra["sl"] is not None:
        fields.append(
            {"name": "SL", "value": f"${float(extra['sl']):,.2f}", "inline": True}
        )
    if "tp" in extra and extra["tp"] is not None:
        fields.append(
            {"name": "TP", "value": f"${float(extra['tp']):,.2f}", "inline": True}
        )

    title = f"{emoji} {decision.replace('_', ' ').upper()} · {symbol}"
    description = reason or "—"

    embed = {
        "title": title,
        "description": description,
        "color": color,
        "fields": fields,
        "footer": {
            "text": f"Bot #{bot_id} · {timeframe} · trade-copilot",
        },
    }

    payload = {"embeds": [embed]}
    try:
        async with httpx.AsyncClient(timeout=5.0) as c:
            r = await c.post(url, json=payload)
            if r.status_code >= 400:
                logger.warning(
                    "discord webhook %s for %s/%s: %s",
                    r.status_code,
                    decision,
                    symbol,
                    r.text[:200],
                )
    except Exception as exc:  # noqa: BLE001 — never crash the runner
        logger.warning("discord webhook post failed: %s", exc)


def post_decision_fire_and_forget(**kwargs: Any) -> None:
    """Schedule post_decision on the running event loop without awaiting.

    Use this from sync contexts inside the runner where we can't await.
    """
    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        return
    if not loop.is_running():
        return
    asyncio.ensure_future(post_decision(**kwargs))
