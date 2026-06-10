"""Partner daily summary — posts a per-grant EOD digest to each partner's
configured webhook (Discord embed or HMAC-signed JSON).

For every active AccountAccessGrant with a partner_webhook_url + secret,
after the UTC day rolls over, we:

  1. Compute compute_daily_summary scoped to the grant's account.
  2. Render the digest as a Discord embed (if URL is Discord) or HMAC-
     signed JSON (otherwise) using the same dispatch path as
     partner_webhook.emit_partner_event.
  3. POST and log delivery. Failures are surfaced server-side but never
     halt the loop — the next day's run tries again with fresh data.

The loop runs as an asyncio task scheduled in app.main.lifespan. It ticks
every TICK_INTERVAL_SECONDS, publishing once per UTC day per grant. The
"have we already posted today" check uses an in-process set so a restart
re-attempts any missed day (acceptable: Discord shows the post twice at
worst). For persistent dedup across restarts, see TODO in maybe_publish().
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime, timedelta
from typing import Any, Optional

from sqlalchemy import or_

from app.core.crypto import decrypt
from app.db.database import SessionLocal
from app.db.models import (
    AccountAccessGrant,
    TradingAccount,
)
from app.integrations.partner_webhook import (
    _is_discord_url,
    _post_with_retry,
    _signature,
)
from app.monitoring import slippage_tracker

logger = logging.getLogger(__name__)

# Loop tick — every minute we check whether any grant needs a fresh post.
TICK_INTERVAL_SECONDS = 60

# In-process dedup: (grant_id, YYYY-MM-DD) tuples we've already posted.
# Cleared on process restart — acceptable for daily summaries since a
# duplicate Discord post the next morning is benign. Replace with a DB
# table if we need durable dedup later.
_published: set[tuple[int, str]] = set()


def _yesterday_utc(now: datetime) -> datetime:
    """The just-completed UTC day at 00:00 — the window the summary
    reports on (so the 00:01 post covers 00:00…23:59 of yesterday)."""
    today_mid = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return today_mid - timedelta(days=1)


def _build_discord_embed(
    *,
    strategy_label: str,
    account_label: str,
    day_str: str,
    summary: dict[str, Any],
) -> dict[str, Any]:
    """Color-code by net P&L — green if real_pnl_pts > 0, red otherwise.
    The embed surfaces the fields a partner cares about for the daily
    audit: trade count, P&L pair, edge erosion, latency p95."""
    is_win = (summary.get("real_pnl_pts") or 0) > 0
    color = 0x10B981 if is_win else 0xEF4444 if summary.get("trades_closed") else 0x6B7280

    fields = [
        {
            "name": "Trades closed",
            "value": str(summary.get("trades_closed", 0)),
            "inline": True,
        },
        {
            "name": "Signals (rejected / total)",
            "value": f"{summary.get('signals_rejected', 0)} / {summary.get('signals_emitted', 0)}",
            "inline": True,
        },
        {
            "name": "Strategy P&L",
            "value": f"{summary.get('strategy_pnl_pts', 0):+.2f} pts",
            "inline": True,
        },
        {
            "name": "Real P&L",
            "value": f"{summary.get('real_pnl_pts', 0):+.2f} pts",
            "inline": True,
        },
        {
            "name": "Edge erosion",
            "value": (
                f"{summary.get('edge_erosion_pts', 0):+.2f} pts · "
                f"${summary.get('edge_erosion_dollars', 0):+.2f}"
            ),
            "inline": True,
        },
        {
            "name": "Entry slippage (avg / worst)",
            "value": (
                f"{summary.get('avg_entry_slippage_pts', 0):+.2f} / "
                f"{summary.get('worst_entry_slippage_pts', 0):+.2f} pts"
            ),
            "inline": True,
        },
        {
            "name": "Latency (avg / p95 / worst)",
            "value": (
                f"{summary.get('avg_total_latency_ms', 0)} / "
                f"{summary.get('p95_total_latency_ms', 0)} / "
                f"{summary.get('worst_total_latency_ms', 0)} ms"
            ),
            "inline": False,
        },
    ]
    return {
        "embeds": [
            {
                "title": f"Daily audit · {strategy_label} · {account_label}",
                "description": f"UTC day: {day_str}",
                "color": color,
                "fields": fields,
                "timestamp": datetime.utcnow().isoformat() + "Z",
            }
        ]
    }


def _build_json_payload(
    *,
    grant_id: int,
    account_id: int,
    tradelocker_account_id: str,
    day_str: str,
    summary: dict[str, Any],
) -> dict[str, Any]:
    """Standard JSON payload posted to non-Discord URLs. Mirrors the
    per-trade event schema: top-level `event` + correlation fields, with
    the daily summary nested under `summary`."""
    return {
        "event": "daily_summary",
        "ts": datetime.utcnow().isoformat() + "Z",
        "grant_id": grant_id,
        "account_id": account_id,
        "tradelocker_account_id": tradelocker_account_id,
        "day": day_str,
        "summary": summary,
    }


async def publish_for_grant(
    grant: AccountAccessGrant,
    day: datetime,
    *,
    db=None,
) -> bool:
    """Compute and POST the daily summary for a single grant. Returns
    True on at least one successful 2xx delivery, False otherwise.

    Re-derives the (owner_user_id, tradelocker_account_id) from the
    TradingAccount on every call rather than caching — grants can be
    revoked or pointed elsewhere between days.
    """
    own_session = db is None
    if own_session:
        db = SessionLocal()
    try:
        ta = db.get(TradingAccount, grant.account_id)
        if ta is None or not ta.is_active:
            return False
        url = grant.partner_webhook_url
        if not url or not grant.partner_webhook_secret_encrypted:
            return False
        try:
            secret = decrypt(grant.partner_webhook_secret_encrypted)
        except Exception:
            logger.warning("daily summary: cannot decrypt secret grant=%s", grant.id)
            return False

        summary = slippage_tracker.compute_daily_summary(
            ta.owner_user_id,
            day=day,
            account_id=ta.tradelocker_account_id,
            db=db,
        )

        day_str = summary.get("day", day.date().isoformat())
        if _is_discord_url(url):
            body_dict = _build_discord_embed(
                strategy_label="all strategies",
                account_label=ta.label or f"acct {ta.tradelocker_account_id}",
                day_str=day_str,
                summary=summary,
            )
            headers = {"Content-Type": "application/json"}
        else:
            body_dict = _build_json_payload(
                grant_id=grant.id,
                account_id=ta.id,
                tradelocker_account_id=ta.tradelocker_account_id,
                day_str=day_str,
                summary=summary,
            )
            body_bytes = json.dumps(body_dict, sort_keys=True).encode("utf-8")
            headers = {
                "Content-Type": "application/json",
                "X-Event": "daily_summary",
                "X-Timestamp": str(int(time.time() * 1000)),
                "X-Signature": _signature(secret, body_bytes),
                "X-Grant-Id": str(grant.id),
            }
            body = body_bytes
            status, err = await _post_with_retry(url, body, headers)
            if err:
                logger.warning(
                    "daily summary grant=%s day=%s: %s (status=%s)",
                    grant.id,
                    day_str,
                    err,
                    status,
                )
                return False
            logger.info(
                "daily summary grant=%s day=%s posted (status=%s)",
                grant.id,
                day_str,
                status,
            )
            return True

        # Discord branch (no HMAC headers).
        body = json.dumps(body_dict).encode("utf-8")
        status, err = await _post_with_retry(url, body, headers)
        if err:
            logger.warning(
                "daily summary grant=%s day=%s (Discord): %s (status=%s)",
                grant.id,
                day_str,
                err,
                status,
            )
            return False
        logger.info(
            "daily summary grant=%s day=%s posted to Discord (status=%s)",
            grant.id,
            day_str,
            status,
        )
        return True
    finally:
        if own_session:
            db.close()


def _list_active_grants(db) -> list[AccountAccessGrant]:
    now = datetime.utcnow()
    return list(
        db.query(AccountAccessGrant)
        .filter(
            AccountAccessGrant.partner_webhook_url.is_not(None),
            AccountAccessGrant.partner_webhook_secret_encrypted.is_not(None),
            AccountAccessGrant.revoked_at.is_(None),
            or_(
                AccountAccessGrant.expires_at.is_(None),
                AccountAccessGrant.expires_at > now,
            ),
        )
        .all()
    )


async def maybe_publish(now: Optional[datetime] = None) -> int:
    """One-tick worker: publish today's summary for any grant that hasn't
    received one yet for the just-completed UTC day. Returns the number of
    grants posted to."""
    now_utc = now or datetime.utcnow()
    # Only consider publishing once we're past 00:00 UTC of the current day
    # (which means yesterday is complete). All ticks during the day are
    # valid — we publish for "yesterday" relative to now.
    target_day = _yesterday_utc(now_utc)
    day_key = target_day.date().isoformat()

    db = SessionLocal()
    try:
        grants = _list_active_grants(db)
    finally:
        db.close()

    posted = 0
    for g in grants:
        if (g.id, day_key) in _published:
            continue
        try:
            ok = await publish_for_grant(g, target_day)
        except Exception as exc:
            logger.warning(
                "daily summary publish_for_grant failed grant=%s: %s",
                g.id,
                exc,
            )
            ok = False
        # Mark as published whether it succeeded or not — we don't want to
        # spam the partner with retries every minute if their endpoint is
        # down. Next day rolls in and we try again.
        _published.add((g.id, day_key))
        if ok:
            posted += 1
    return posted


async def partner_daily_summary_task() -> None:
    """Background loop entry point. Cancelled on shutdown."""
    while True:
        try:
            await maybe_publish()
        except Exception as exc:
            logger.warning("partner_daily_summary tick raised: %s", exc)
        try:
            await asyncio.sleep(TICK_INTERVAL_SECONDS)
        except asyncio.CancelledError:
            return
