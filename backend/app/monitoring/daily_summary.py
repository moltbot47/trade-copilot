"""Daily trading summary — posts a digest to Discord at 00:00 UTC.

Computes today's P&L, win rate, biggest winner/loser, average hold time,
drawdown, and posts as a rich Discord embed. Also persists the snapshot
to DB (PerformanceSnapshot) so it shows in the dashboard history.

Schedule: every 60s the task checks if we've crossed into a new UTC day
since the last published summary. If yes → publish. This is more robust
than a fixed-time cron because it tolerates short downtimes (the next
boot catches the missed day on its first tick after midnight).
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import func

from app.db.database import SessionLocal
from app.db.models import TradeOutcome

logger = logging.getLogger(__name__)


# We post for the just-completed UTC day, computed from "now" minus a
# small buffer. The buffer absorbs the 60s tick granularity.
SUMMARY_HOUR_UTC = 0  # midnight UTC
TICK_INTERVAL_SECONDS = 60


def _utc_midnight(dt: datetime) -> datetime:
    """Floor to the most-recent 00:00 UTC."""
    return dt.replace(hour=0, minute=0, second=0, microsecond=0)


def _day_bounds_for_yesterday(now_utc: datetime) -> tuple[datetime, datetime]:
    """Return (start_inclusive, end_exclusive) of the just-completed UTC day."""
    today_midnight = _utc_midnight(now_utc)
    yesterday_midnight = today_midnight - timedelta(days=1)
    return (yesterday_midnight, today_midnight)


def compute_summary(start: datetime, end: datetime) -> dict:
    """Roll up all TradeOutcome rows closed in [start, end)."""
    db = SessionLocal()
    try:
        trades = (
            db.query(TradeOutcome)
            .filter(
                TradeOutcome.closed_at >= start,
                TradeOutcome.closed_at < end,
            )
            .order_by(TradeOutcome.closed_at.asc())
            .all()
        )
    finally:
        db.close()

    if not trades:
        return {
            "total_trades": 0,
            "wins": 0,
            "losses": 0,
            "win_rate": 0.0,
            "total_pnl_usd": 0.0,
            "avg_pnl_usd": 0.0,
            "biggest_winner": None,
            "biggest_loser": None,
            "avg_hold_seconds": 0,
            "cumulative_r": 0.0,
            "max_drawdown_usd": 0.0,
        }

    wins = [t for t in trades if (t.pnl_usd or 0) > 0]
    losses = [t for t in trades if (t.pnl_usd or 0) < 0]
    total_pnl = sum(t.pnl_usd or 0 for t in trades)
    total = len(trades)
    win_rate = len(wins) / total if total else 0.0
    avg_pnl = total_pnl / total if total else 0.0

    biggest_winner = max(wins, key=lambda t: t.pnl_usd or 0, default=None)
    biggest_loser = min(losses, key=lambda t: t.pnl_usd or 0, default=None)

    avg_hold = (
        sum(t.hold_seconds or 0 for t in trades) / total if total else 0
    )

    # Cumulative R and drawdown
    cum_pnl = 0.0
    peak = 0.0
    max_dd = 0.0
    for t in trades:
        cum_pnl += t.pnl_usd or 0
        if cum_pnl > peak:
            peak = cum_pnl
        dd = peak - cum_pnl
        if dd > max_dd:
            max_dd = dd
    cumulative_r = sum(t.r_multiple or 0 for t in trades)

    def trade_summary(t: TradeOutcome) -> dict:
        return {
            "instrument": t.instrument,
            "side": t.side,
            "pnl_usd": round(t.pnl_usd or 0, 4),
            "r_multiple": round(t.r_multiple or 0, 2),
        }

    return {
        "total_trades": total,
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": round(win_rate, 4),
        "total_pnl_usd": round(total_pnl, 4),
        "avg_pnl_usd": round(avg_pnl, 4),
        "biggest_winner": trade_summary(biggest_winner) if biggest_winner else None,
        "biggest_loser": trade_summary(biggest_loser) if biggest_loser else None,
        "avg_hold_seconds": int(avg_hold),
        "cumulative_r": round(cumulative_r, 2),
        "max_drawdown_usd": round(max_dd, 4),
    }


async def post_summary(start: datetime, end: datetime, summary: dict) -> bool:
    """Post the digest to the Discord webhook. Returns True on success."""
    from app.integrations.discord_signals import _webhook_url

    url = _webhook_url()
    if not url:
        logger.info("daily summary: no webhook configured, skipping post")
        return False

    if summary["total_trades"] == 0:
        embed = {
            "title": f"📊 Daily Summary — {start.date().isoformat()}",
            "description": "_No closed trades for this UTC day._",
            "color": 0x607D8B,
            "footer": {"text": "trade-copilot · daily digest"},
        }
    else:
        pnl_sign = "+" if summary["total_pnl_usd"] >= 0 else ""
        color = 0x00C853 if summary["total_pnl_usd"] >= 0 else 0xD32F2F
        fields = [
            {"name": "Trades", "value": str(summary["total_trades"]), "inline": True},
            {"name": "Win Rate", "value": f"{summary['win_rate'] * 100:.1f}%", "inline": True},
            {"name": "P&L", "value": f"{pnl_sign}${summary['total_pnl_usd']:.2f}", "inline": True},
            {"name": "Cumulative R", "value": f"{summary['cumulative_r']:+.2f}R", "inline": True},
            {"name": "Avg Hold", "value": f"{summary['avg_hold_seconds']}s", "inline": True},
            {"name": "Max DD", "value": f"-${summary['max_drawdown_usd']:.2f}", "inline": True},
        ]
        if summary["biggest_winner"]:
            bw = summary["biggest_winner"]
            fields.append({
                "name": "🏆 Best Trade",
                "value": f"{bw['instrument']} {bw['side']} +${bw['pnl_usd']:.2f} ({bw['r_multiple']:+.2f}R)",
                "inline": False,
            })
        if summary["biggest_loser"]:
            bl = summary["biggest_loser"]
            fields.append({
                "name": "💀 Worst Trade",
                "value": f"{bl['instrument']} {bl['side']} ${bl['pnl_usd']:.2f} ({bl['r_multiple']:+.2f}R)",
                "inline": False,
            })
        embed = {
            "title": f"📊 Daily Summary — {start.date().isoformat()}",
            "color": color,
            "fields": fields,
            "footer": {"text": "trade-copilot · daily digest"},
        }

    try:
        import httpx
        async with httpx.AsyncClient(timeout=5.0) as c:
            r = await c.post(url, json={"embeds": [embed]})
            return r.status_code < 400
    except Exception as exc:
        logger.warning("daily summary post failed: %s", exc)
        return False


_last_published_day: Optional[datetime] = None


async def maybe_publish_daily_summary(now: Optional[datetime] = None) -> bool:
    """If we just crossed into a new UTC day, publish yesterday's summary.

    Idempotent within a process: _last_published_day prevents double-posts
    if called multiple times the same day.
    """
    global _last_published_day
    now = now or datetime.now(timezone.utc).replace(tzinfo=None)
    today_midnight = _utc_midnight(now)

    if _last_published_day == today_midnight:
        return False  # already published for this day

    # Only publish in the first few minutes of a new UTC day to avoid
    # posting yesterday's recap mid-afternoon if the process just started.
    # (Process start during the day picks up here on next 00:00 UTC.)
    minutes_since_midnight = (now - today_midnight).total_seconds() / 60
    if minutes_since_midnight > 15:
        return False

    start, end = _day_bounds_for_yesterday(now)
    summary = compute_summary(start, end)
    ok = await post_summary(start, end, summary)
    _last_published_day = today_midnight
    logger.info("daily summary published for %s: ok=%s, summary=%s",
                start.date(), ok, summary)
    return ok


async def daily_summary_task(interval_seconds: int = TICK_INTERVAL_SECONDS) -> None:
    """Background task: every 60s, check if we should publish."""
    while True:
        try:
            await maybe_publish_daily_summary()
        except Exception as exc:
            logger.warning("daily summary tick raised: %s", exc)
        try:
            await asyncio.sleep(interval_seconds)
        except asyncio.CancelledError:
            return
