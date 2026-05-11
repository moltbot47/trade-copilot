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
import time
from datetime import datetime, timezone
from typing import Any

import httpx

logger = logging.getLogger(__name__)

# In-process cache for the rolling W/L counter. The Discord publisher fires
# once per tick (~2/min on 1m × 2 symbols), so a 30-sec TTL is plenty —
# avoids hammering the DB with the same COUNT(*) twice per minute.
_COUNTER_CACHE: dict[int, tuple[float, dict | None]] = {}
_COUNTER_TTL_SECONDS = 30.0

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


def _webhook_url(user_id: int | None = None) -> str | None:
    """Return the webhook URL for a given user, falling back to the global env.

    Priority: user.discord_webhook_url (if user_id given) > env vars > None.
    Per-user webhooks let each customer route their bot's signals to their
    own private channel, instead of everyone sharing the operator's webhook.
    """
    if user_id is not None:
        try:
            from app.db.database import SessionLocal
            from app.db.models import User
            db = SessionLocal()
            try:
                u = db.query(User).filter(User.id == user_id).first()
                if u and u.discord_webhook_url:
                    return u.discord_webhook_url
            finally:
                db.close()
        except Exception:
            pass  # fall through to env
    for name in _DISCORD_WEBHOOK_ENV_NAMES:
        v = os.environ.get(name)
        if v:
            return v
    return None


def _rolling_counter(bot_id: int) -> dict | None:
    """Return wins/losses/total/win-rate for this bot from the TradeOutcome
    table. Cached for _COUNTER_TTL_SECONDS to avoid repeat queries on
    every tick. Returns None if there are no closed trades yet.
    """
    now = time.monotonic()
    cached = _COUNTER_CACHE.get(bot_id)
    if cached is not None:
        ts, payload = cached
        if now - ts < _COUNTER_TTL_SECONDS:
            return payload

    # Lazy DB import — keeps this module importable from non-app contexts.
    try:
        from app.db.database import SessionLocal
        from app.db.models import TradeOutcome
        from sqlalchemy import case, func
    except Exception as exc:
        logger.debug("rolling counter skipped (db not importable): %s", exc)
        return None

    db = SessionLocal()
    try:
        # Single aggregate query: total, wins, losses, sum_pnl
        wins_expr = func.sum(case((TradeOutcome.pnl_usd > 0, 1), else_=0))
        losses_expr = func.sum(case((TradeOutcome.pnl_usd < 0, 1), else_=0))
        row = (
            db.query(
                func.count(TradeOutcome.id),
                wins_expr,
                losses_expr,
                func.sum(TradeOutcome.pnl_usd),
            )
            .filter(TradeOutcome.bot_id == bot_id)
            .one_or_none()
        )
        if not row or not row[0]:
            payload = None
        else:
            total = int(row[0] or 0)
            wins = int(row[1] or 0)
            losses = int(row[2] or 0)
            total_pnl = float(row[3] or 0.0)
            win_rate = (wins / total) if total > 0 else 0.0
            payload = {
                "total": total,
                "wins": wins,
                "losses": losses,
                "win_rate": win_rate,
                "total_pnl": total_pnl,
            }
    except Exception as exc:
        logger.debug("rolling counter query failed: %s", exc)
        payload = None
    finally:
        db.close()

    _COUNTER_CACHE[bot_id] = (now, payload)
    return payload


async def post_decision(
    decision: str,
    *,
    bot_id: int,
    timeframe: str,
    symbol: str,
    user_id: int | None = None,
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

    If ``user_id`` is provided AND that user has set a personal
    discord_webhook_url, the post goes there instead of the global webhook.
    Falls back to global if the per-user URL fails.
    """
    if decision not in HIGH_SIGNAL_DECISIONS:
        return
    url = _webhook_url(user_id)
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

    # Rolling W/L counter — appended to the footer so every post shows the
    # bot's career record. "12W / 8L · 60.0% · +$23.45" style.
    counter = _rolling_counter(bot_id)
    if counter:
        pnl_sign = "+" if counter["total_pnl"] >= 0 else ""
        record = (
            f"{counter['wins']}W / {counter['losses']}L"
            f" · {counter['win_rate'] * 100:.1f}% WR"
            f" · {counter['total']} total"
            f" · {pnl_sign}${counter['total_pnl']:.2f} P&L"
        )
        footer_text = f"{record} | Bot #{bot_id} · {timeframe}"
    else:
        footer_text = f"no closed trades yet | Bot #{bot_id} · {timeframe}"

    embed = {
        "title": title,
        "description": description,
        "color": color,
        "fields": fields,
        "footer": {
            "text": footer_text,
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


# -----------------------------------------------------------------------
# Admin-event alerts — signup, login, broker connect
# -----------------------------------------------------------------------
#
# These post to the GLOBAL DISCORD_WEBHOOK_URL (operator/signals channel),
# never to a per-user webhook. Per-user routing would leak the operator's
# customer base to each customer, which is the wrong privacy model.
#
# Every post is best-effort + fire-and-forget — login latency must not
# block on a Discord round trip, and a Discord outage must not cascade
# into the auth flow.

# Visual styling for each admin event. Color is Discord embed color.
_ADMIN_EVENT_STYLE: dict[str, tuple[str, int]] = {
    "signup":         ("🆕 **New signup**",          0x4CAF50),  # green
    "login":          ("🔑 Login",                   0x546E7A),  # muted blue-grey
    "broker_connect": ("🔌 **Broker connected**",    0x00BCD4),  # cyan
    "broker_reconnect": ("🔁 Broker reconnected",    0x546E7A),  # muted
}


async def post_admin_event(
    event: str,
    *,
    user_email: str,
    details: dict[str, Any] | None = None,
) -> None:
    """Send an admin-channel notification for a notable user event.

    ``event`` is one of the keys in ``_ADMIN_EVENT_STYLE`` (signup, login,
    broker_connect, broker_reconnect). Unknown events are posted with a
    neutral style so we never silently drop a future event type.
    """
    url = _webhook_url()  # global only — see module-level comment
    if not url:
        return  # admin alerts disabled (no DISCORD_WEBHOOK_URL configured)

    label, color = _ADMIN_EVENT_STYLE.get(event, (f"ℹ️ {event}", 0x9E9E9E))
    fields: list[dict[str, Any]] = [
        {"name": "User", "value": f"`{user_email}`", "inline": True},
    ]
    if details:
        for k, v in details.items():
            # Trim long values so the embed renders cleanly on mobile.
            s = str(v)
            if len(s) > 256:
                s = s[:253] + "…"
            fields.append({"name": k, "value": s, "inline": True})

    embed = {
        "title": label,
        "color": color,
        "fields": fields,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

    try:
        async with httpx.AsyncClient(timeout=4.0) as client:
            r = await client.post(url, json={"embeds": [embed]})
            if r.status_code >= 400:
                logger.warning(
                    "discord admin alert returned %s for %s/%s: %s",
                    r.status_code, event, user_email, r.text[:200],
                )
    except Exception as exc:  # noqa: BLE001 — never block auth on Discord
        logger.warning("discord admin alert (%s/%s) failed: %s", event, user_email, exc)


def post_admin_event_fire_and_forget(**kwargs: Any) -> None:
    """Schedule post_admin_event on the running loop without awaiting.

    Use from sync endpoint handlers — login / connect endpoints are sync.
    """
    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        return
    if not loop.is_running():
        return
    asyncio.ensure_future(post_admin_event(**kwargs))
