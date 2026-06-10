"""Partner webhook hook — tees slippage_records lifecycle events to a
partner-controlled HTTPS endpoint signed with their HMAC secret.

The trust anchor of the profit-share / audit relationship. The partner
keeps an independent mirror of every signal / fill / close / rejection
without ever needing to trust our dashboard's computed numbers; they
recompute slippage and P&L from the raw broker JSON we forward.

Lifecycle integration:

  IsolatedRunner._fire (signal emit)   → emit_partner_event("signal", id)
  IsolatedRunner._fire (after fill)    → emit_partner_event("fill", id)
  PositionMonitor._close_outcome       → emit_partner_event("close", id)
  slippage_tracker.mark_rejected       → emit_partner_event("rejected", id)

Discovery — for a given SlippageRecord we look up every active, non-
expired AccountAccessGrant on the matching TradingAccount and dispatch
to every grant that has both partner_webhook_url and a secret set.

Payload format per docs/PARTNER_STRATEGY_SPEC.md §5 — JSON body with raw
broker response included as a field. Signed with:

  X-Signature: sha256=<hex>
  X-Timestamp: <unix-ms>
  X-Event: signal|fill|close|rejected

Discord URLs (https://discord.com/api/webhooks/* or discordapp.com) are
detected automatically and formatted as readable embeds instead of raw
JSON — lets partners point at a private Discord channel for zero-infra
monitoring.

Reliability — retries with exponential backoff (1s, 4s, 16s) on 5xx or
network error. Any failure is logged but never blocks trading. Outbound
calls are fire-and-forget via asyncio.create_task at the call site.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import time
from datetime import datetime
from typing import Any

import httpx
from sqlalchemy import and_, or_

from app.core.crypto import decrypt
from app.db.database import SessionLocal
from app.db.models import (
    AccountAccessGrant,
    SlippageRecord,
    TradingAccount,
)

logger = logging.getLogger(__name__)


# Retry schedule for transient HTTP failures (seconds). 3 attempts.
_RETRY_DELAYS_SEC = (1.0, 4.0, 16.0)

# Discord URL prefixes — match either canonical or legacy domain.
_DISCORD_PREFIXES = (
    "https://discord.com/api/webhooks/",
    "https://discordapp.com/api/webhooks/",
)


def _is_discord_url(url: str) -> bool:
    return url.startswith(_DISCORD_PREFIXES)


def _signature(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(
        secret.encode(), body, hashlib.sha256
    ).hexdigest()


# ----------------------------------------------------------------------- #
# payload builders
# ----------------------------------------------------------------------- #
def _base_payload(event: str, record: SlippageRecord) -> dict[str, Any]:
    """Fields common to every event type. Includes correlation id (the
    slippage_record id) so a partner's downstream pipeline can join
    signal→fill→close on the same trade."""
    return {
        "event": event,
        "slippage_record_id": record.id,
        "ts": datetime.utcnow().isoformat() + "Z",
        "strategy": record.strategy_name,
        "account_id": record.account_id,
        "symbol": record.symbol,
        "side": record.side,
    }


def build_payload(event: str, record: SlippageRecord) -> dict[str, Any]:
    """Construct the event-specific JSON body. Per the partner spec §5
    each event publishes only the fields meaningful at that point."""
    body = _base_payload(event, record)

    if event == "signal":
        body.update({
            "bar_close_ts": record.bar_close_ts.isoformat() + "Z" if record.bar_close_ts else None,
            "bar_close_price": record.bar_close_price,
            "expected_entry_price": record.expected_entry_price,
            "hard_stop_distance_pts": record.hard_stop_distance_pts,
            "trailing_stop_distance_pts": record.trailing_stop_distance_pts,
            "early_stop_condition": record.early_stop_condition,
        })
    elif event == "fill":
        body.update({
            "expected_entry_price": record.expected_entry_price,
            "actual_entry_price": record.actual_entry_price,
            "entry_slippage_pts": record.entry_slippage_pts,
            "signal_latency_ms": record.signal_latency_ms,
            "submit_latency_ms": record.submit_latency_ms,
            "broker_ack_latency_ms": record.broker_ack_latency_ms,
            "fill_latency_ms": record.fill_latency_ms,
            "total_latency_ms": record.total_latency_ms,
            "broker_raw_response": (
                json.loads(record.broker_fill_response_json)
                if record.broker_fill_response_json
                else None
            ),
        })
    elif event == "close":
        body.update({
            "exit_type": record.exit_type,
            "peak_price": record.peak_price,
            "expected_exit_price": record.expected_exit_price,
            "actual_exit_price": record.actual_exit_price,
            "exit_slippage_pts": record.exit_slippage_pts,
            "strategy_pnl_pts": record.strategy_pnl_pts,
            "real_pnl_pts": record.real_pnl_pts,
            "slippage_total_pts": record.slippage_total_pts,
            "slippage_total_dollars": record.slippage_total_dollars,
            "actual_entry_price": record.actual_entry_price,
            "broker_raw_response": (
                json.loads(record.broker_close_response_json)
                if record.broker_close_response_json
                else None
            ),
        })
    elif event == "rejected":
        body.update({
            "expected_entry_price": record.expected_entry_price,
            "reason": "broker rejected or runner gave up",
        })

    return body


def build_discord_payload(event: str, record: SlippageRecord) -> dict[str, Any]:
    """Discord webhook format — embed instead of raw fields so a partner
    pointing at a Discord channel gets readable messages without parsing
    JSON. Color-coded by event type so close/win is green, close/loss is
    red, signal is amber, fill is blue, rejected is grey."""
    color_map = {
        "signal": 0xF59E0B,
        "fill": 0x3B82F6,
        "rejected": 0x6B7280,
    }
    if event == "close":
        is_win = (record.real_pnl_pts or 0) > 0
        color = 0x10B981 if is_win else 0xEF4444
    else:
        color = color_map.get(event, 0x6B7280)

    fields: list[dict[str, Any]] = [
        {"name": "Strategy", "value": record.strategy_name, "inline": True},
        {"name": "Symbol", "value": f"{record.symbol} ({record.side})", "inline": True},
    ]

    if event == "signal":
        fields += [
            {
                "name": "Expected entry",
                "value": f"{record.expected_entry_price:.2f}",
                "inline": True,
            },
            {
                "name": "Hard stop",
                "value": f"{record.hard_stop_distance_pts:.2f} pts",
                "inline": True,
            },
            {
                "name": "Trailing",
                "value": f"{record.trailing_stop_distance_pts:.2f} pts",
                "inline": True,
            },
        ]
    elif event == "fill":
        fields += [
            {
                "name": "Filled at",
                "value": f"{record.actual_entry_price:.2f}",
                "inline": True,
            },
            {
                "name": "Slippage",
                "value": f"{record.entry_slippage_pts:+.2f} pts",
                "inline": True,
            },
            {
                "name": "Latency",
                "value": f"{record.total_latency_ms} ms",
                "inline": True,
            },
        ]
    elif event == "close":
        fields += [
            {
                "name": "Exit type",
                "value": str(record.exit_type or "?"),
                "inline": True,
            },
            {
                "name": "Real P&L",
                "value": f"{record.real_pnl_pts:+.2f} pts",
                "inline": True,
            },
            {
                "name": "Edge erosion",
                "value": (
                    f"{record.slippage_total_pts:+.2f} pts"
                    if record.slippage_total_pts is not None
                    else "n/a"
                ),
                "inline": True,
            },
        ]

    return {
        "embeds": [
            {
                "title": f"[{event.upper()}] {record.strategy_name} · {record.symbol}",
                "color": color,
                "fields": fields,
                "footer": {"text": f"record #{record.id}"},
                "timestamp": datetime.utcnow().isoformat() + "Z",
            }
        ]
    }


# ----------------------------------------------------------------------- #
# dispatcher
# ----------------------------------------------------------------------- #
def _list_matching_grants(record: SlippageRecord, db) -> list[AccountAccessGrant]:
    """Active grants on the TradingAccount whose owner+TL-account match the
    record. Webhook is only fired for grants that have both URL and secret
    configured and are not expired or revoked."""
    now = datetime.utcnow()
    rows = (
        db.query(AccountAccessGrant)
        .join(TradingAccount, AccountAccessGrant.account_id == TradingAccount.id)
        .filter(
            and_(
                TradingAccount.owner_user_id == record.user_id,
                TradingAccount.tradelocker_account_id == record.account_id,
                AccountAccessGrant.partner_webhook_url.is_not(None),
                AccountAccessGrant.partner_webhook_secret_encrypted.is_not(None),
                AccountAccessGrant.revoked_at.is_(None),
                or_(
                    AccountAccessGrant.expires_at.is_(None),
                    AccountAccessGrant.expires_at > now,
                ),
            )
        )
        .all()
    )
    return rows


async def _post_with_retry(
    url: str, body: bytes, headers: dict[str, str]
) -> tuple[int | None, str | None]:
    """Send body to url with exponential backoff. Returns (status, error)."""
    last_status: int | None = None
    last_err: str | None = None
    for attempt, delay in enumerate(_RETRY_DELAYS_SEC):
        try:
            async with httpx.AsyncClient(timeout=10.0) as c:
                r = await c.post(url, content=body, headers=headers)
            last_status = r.status_code
            if 200 <= r.status_code < 300:
                return r.status_code, None
            # 4xx is permanent — no retry (likely bad URL or 401 on a
            # Discord webhook whose token was rotated).
            if 400 <= r.status_code < 500:
                return r.status_code, f"client error {r.status_code}"
        except httpx.HTTPError as exc:
            last_err = str(exc)
        # 5xx or transport error — back off and retry unless this was the
        # last attempt.
        if attempt < len(_RETRY_DELAYS_SEC) - 1:
            await asyncio.sleep(delay)
    return last_status, last_err or "all retries exhausted"


# ----------------------------------------------------------------------- #
# public entry point
# ----------------------------------------------------------------------- #
async def emit_partner_event(
    event: str,
    slippage_record_id: int,
    *,
    db=None,
) -> int:
    """Fan a slippage_records event out to every matching partner webhook.

    Returns the number of webhooks the platform delivered (or attempted —
    failures are logged but counted as delivery attempts). Suitable for
    use inside ``asyncio.create_task(...)`` from the runner so the trade
    path isn't blocked on outbound HTTP.

    ``event`` must be one of: "signal" | "fill" | "close" | "rejected".
    """
    if event not in ("signal", "fill", "close", "rejected"):
        logger.warning("emit_partner_event: unknown event=%s", event)
        return 0

    own_session = db is None
    if own_session:
        db = SessionLocal()
    try:
        record = db.get(SlippageRecord, slippage_record_id)
        if record is None:
            logger.warning(
                "emit_partner_event: slippage_record %s not found",
                slippage_record_id,
            )
            return 0

        grants = _list_matching_grants(record, db)
        if not grants:
            return 0

        ts_ms = str(int(time.time() * 1000))
        # Build the standard payload once; per-grant we either send it or
        # swap for a Discord embed.
        std_payload = build_payload(event, record)
        std_body = json.dumps(std_payload, sort_keys=True).encode("utf-8")

        sent = 0
        for g in grants:
            try:
                secret = decrypt(g.partner_webhook_secret_encrypted)
            except Exception as exc:
                logger.warning(
                    "emit_partner_event: cannot decrypt secret for grant=%s: %s",
                    g.id,
                    exc,
                )
                continue
            url = g.partner_webhook_url
            if not url or not secret:
                continue

            if _is_discord_url(url):
                body = json.dumps(build_discord_payload(event, record)).encode("utf-8")
                headers = {"Content-Type": "application/json"}
            else:
                headers = {
                    "Content-Type": "application/json",
                    "X-Event": event,
                    "X-Timestamp": ts_ms,
                    "X-Signature": _signature(secret, std_body),
                    "X-Grant-Id": str(g.id),
                }
                body = std_body

            status, err = await _post_with_retry(url, body, headers)
            sent += 1
            if err:
                logger.warning(
                    "partner webhook grant=%s event=%s rec=%s: %s (status=%s)",
                    g.id,
                    event,
                    record.id,
                    err,
                    status,
                )
            else:
                logger.info(
                    "partner webhook grant=%s event=%s rec=%s ok (status=%s)",
                    g.id,
                    event,
                    record.id,
                    status,
                )
        return sent
    finally:
        if own_session:
            db.close()


def schedule_emit(event: str, slippage_record_id: int) -> None:
    """Fire-and-forget wrapper for runners that don't want to await the
    HTTP roundtrips. Schedules emit_partner_event on the running event
    loop; no-op if there is no loop (e.g. unit-test threads). Safe to call
    from sync code paths like slippage_tracker.mark_rejected."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return  # no event loop, e.g. tests not using asyncio
    loop.create_task(emit_partner_event(event, slippage_record_id))
