"""Partner-scoped read API — partner dashboard + audit data access.

Every endpoint here scopes responses to the calling user's *accessible*
TradingAccounts: accounts they own + accounts where they hold an active,
non-expired, non-revoked AccountAccessGrant. The scoping is enforced by
the existing helpers in app/core/account_access.py — same gate the DOM
endpoints use, same unified-404 enumeration defense.

For Vladimir's profit-share audit:

  1. Owner (you) creates a TradingAccount + grants Vladimir role=viewer
     with allowed_instruments_csv="NAS100" + expires_at=audit-end + a
     partner_webhook_url/secret.
  2. Vladimir logs in with his own User account.
  3. These endpoints return only the data tied to that grant; he cannot
     see your other accounts, other strategies, or other users.
  4. Single-record reads include the raw TradeLocker broker JSON so he
     can recompute slippage / P&L independently — that's the trust
     anchor for the profit-share math.
"""
from __future__ import annotations

import json
import logging
from datetime import date as date_cls, datetime, timedelta
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import and_, or_
from sqlalchemy.orm import Session

from app.api.users import get_current_user
from app.core.account_access import (
    list_accessible_accounts,
    may_view,
    resolve_account_for_action,
)
from app.core.tradelocker_auth import call_with_refresh
from app.core.tradelocker_client import TradeLockerClient, TradeLockerError
from app.db.database import get_db
from app.db.models import (
    AccountAccessGrant,
    SlippageRecord,
    TradingAccount,
    User,
)
from app.monitoring import slippage_tracker

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/partner", tags=["partner"])


# ----------------------------------------------------------------------- #
# helpers
# ----------------------------------------------------------------------- #
def _partner_owner_tlid_pairs(
    db: Session, partner: User
) -> list[tuple[int, str]]:
    """For each TradingAccount the partner can view, return
    (owner_user_id, tradelocker_account_id) — the join keys SlippageRecord
    is filtered by (record.user_id, record.account_id).
    """
    accounts = list_accessible_accounts(db, partner)
    pairs: list[tuple[int, str]] = []
    for a in accounts:
        ta = db.get(TradingAccount, a["id"])
        if ta is None:
            continue
        pairs.append((ta.owner_user_id, ta.tradelocker_account_id))
    return pairs


def _resolve_slippage_record_or_404(
    db: Session, partner: User, record_id: int
) -> SlippageRecord:
    """Single-record access check: the record's (user_id, tl_acct_id) must
    match a TradingAccount the partner can view. Returns 404 for missing
    AND inaccessible — symmetrical with the rest of the partner API."""
    NOT_FOUND = HTTPException(status_code=404, detail="record not found")
    rec = db.get(SlippageRecord, record_id)
    if rec is None:
        raise NOT_FOUND
    ta = (
        db.query(TradingAccount)
        .filter(
            TradingAccount.owner_user_id == rec.user_id,
            TradingAccount.tradelocker_account_id == rec.account_id,
        )
        .first()
    )
    if ta is None or not may_view(db, partner, ta):
        raise NOT_FOUND
    return rec


def _serialize_record_summary(r: SlippageRecord) -> dict[str, Any]:
    """Light row for list endpoints — excludes raw broker JSON which can
    be megabytes per trade."""
    return {
        "id": r.id,
        "status": r.status,
        "strategy": r.strategy_name,
        "account_id": r.account_id,
        "symbol": r.symbol,
        "side": r.side,
        "bar_close_ts": r.bar_close_ts.isoformat() + "Z" if r.bar_close_ts else None,
        "signal_ts": r.signal_ts.isoformat() + "Z" if r.signal_ts else None,
        "fill_ts": r.fill_ts.isoformat() + "Z" if r.fill_ts else None,
        "closed_ts": r.closed_ts.isoformat() + "Z" if r.closed_ts else None,
        "expected_entry_price": r.expected_entry_price,
        "actual_entry_price": r.actual_entry_price,
        "entry_slippage_pts": r.entry_slippage_pts,
        "exit_type": r.exit_type,
        "actual_exit_price": r.actual_exit_price,
        "exit_slippage_pts": r.exit_slippage_pts,
        "strategy_pnl_pts": r.strategy_pnl_pts,
        "real_pnl_pts": r.real_pnl_pts,
        "slippage_total_pts": r.slippage_total_pts,
        "slippage_total_dollars": r.slippage_total_dollars,
        "total_latency_ms": r.total_latency_ms,
    }


def _serialize_record_full(r: SlippageRecord) -> dict[str, Any]:
    """Full record with raw broker JSON for the detail endpoint — the
    trust anchor for partner-side recompute."""
    summary = _serialize_record_summary(r)
    summary.update({
        "bar_close_price": r.bar_close_price,
        "hard_stop_distance_pts": r.hard_stop_distance_pts,
        "trailing_stop_distance_pts": r.trailing_stop_distance_pts,
        "early_stop_condition": r.early_stop_condition,
        "expected_exit_price": r.expected_exit_price,
        "peak_price": r.peak_price,
        "order_submit_ts": r.order_submit_ts.isoformat() + "Z" if r.order_submit_ts else None,
        "order_ack_ts": r.order_ack_ts.isoformat() + "Z" if r.order_ack_ts else None,
        "signal_latency_ms": r.signal_latency_ms,
        "submit_latency_ms": r.submit_latency_ms,
        "broker_ack_latency_ms": r.broker_ack_latency_ms,
        "fill_latency_ms": r.fill_latency_ms,
        "broker_fill_response": (
            json.loads(r.broker_fill_response_json)
            if r.broker_fill_response_json
            else None
        ),
        "broker_close_response": (
            json.loads(r.broker_close_response_json)
            if r.broker_close_response_json
            else None
        ),
        "extra": json.loads(r.extra_json) if r.extra_json else {},
    })
    return summary


# ----------------------------------------------------------------------- #
# endpoints
# ----------------------------------------------------------------------- #
@router.get("/accounts")
def list_partner_accounts(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    """List every TradingAccount the calling user can view — owned + granted.

    Drives the account picker in the partner dashboard. Each entry shows
    role ("owner" | "trader" | "viewer") and the caps that apply.
    """
    return {"accounts": list_accessible_accounts(db, user)}


@router.get("/slippage-records")
def list_partner_slippage_records(
    since: Optional[datetime] = Query(None, description="ISO timestamp"),
    until: Optional[datetime] = Query(None, description="ISO timestamp"),
    strategy_name: Optional[str] = Query(None),
    account_id: Optional[int] = Query(
        None, description="TradingAccount.id to filter by"
    ),
    status: Optional[str] = Query(
        None, description="pending | open | closed | rejected"
    ),
    limit: int = Query(200, le=1000),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    """Slippage records for accounts the caller can view. Default window
    is the trailing 24h. Excludes raw broker JSON for performance — use
    /slippage-records/{id} for the full record."""
    if since is None:
        since = datetime.utcnow() - timedelta(days=1)
    if until is None:
        until = datetime.utcnow()

    # When account_id is specified, narrow to just that account (after
    # access check). Otherwise enumerate all accessible accounts.
    if account_id is not None:
        resolved = resolve_account_for_action(db, user, account_id, "view")
        pairs = [
            (resolved.account.owner_user_id, resolved.account.tradelocker_account_id)
        ]
    else:
        pairs = _partner_owner_tlid_pairs(db, user)

    if not pairs:
        return {"records": [], "since": since.isoformat() + "Z", "until": until.isoformat() + "Z"}

    conds = or_(
        *[
            and_(
                SlippageRecord.user_id == u,
                SlippageRecord.account_id == a,
            )
            for u, a in pairs
        ]
    )
    q = db.query(SlippageRecord).filter(
        conds,
        SlippageRecord.created_at >= since,
        SlippageRecord.created_at <= until,
    )
    if strategy_name:
        q = q.filter(SlippageRecord.strategy_name == strategy_name)
    if status:
        q = q.filter(SlippageRecord.status == status)
    rows = q.order_by(SlippageRecord.created_at.desc()).limit(limit).all()
    return {
        "records": [_serialize_record_summary(r) for r in rows],
        "since": since.isoformat() + "Z",
        "until": until.isoformat() + "Z",
        "count": len(rows),
    }


@router.get("/slippage-records/{record_id}")
def get_partner_slippage_record(
    record_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    """Single-record detail including the raw broker JSON — the trust
    anchor a partner uses to recompute slippage and P&L independently
    of our computed numbers."""
    rec = _resolve_slippage_record_or_404(db, user, record_id)
    return _serialize_record_full(rec)


@router.get("/daily-summary")
def get_partner_daily_summary(
    account_id: int = Query(..., description="TradingAccount.id"),
    day: Optional[str] = Query(
        None, description="YYYY-MM-DD UTC; defaults to today"
    ),
    strategy_name: Optional[str] = Query(None),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    """EOD aggregate for ONE accessible account — signal counts, P&L pair,
    edge erosion, slippage avg/worst, latency p95/worst. Powers the
    dashboard "today" summary card and the Discord EOD digest.

    Scoped to one TradingAccount so a partner with grants on multiple
    accounts can drill in per-account without cross-account totals.
    """
    resolved = resolve_account_for_action(db, user, account_id, "view")

    if day is not None:
        try:
            day_dt = datetime.fromisoformat(day)
        except ValueError:
            raise HTTPException(400, "day must be YYYY-MM-DD")
    else:
        day_dt = datetime.utcnow()

    # compute_daily_summary scopes by user_id — pass the OWNER's id since
    # SlippageRecord.user_id == owner. The partner's access is already
    # verified by resolve_account_for_action above.
    summary = slippage_tracker.compute_daily_summary(
        resolved.account.owner_user_id,
        day=day_dt,
        strategy_name=strategy_name,
        db=db,
    )
    summary["account_id"] = account_id
    summary["account_label"] = resolved.account.label
    return summary


@router.get("/broker-snapshot/{account_id}")
async def get_partner_broker_snapshot(
    account_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    """Live broker positions + account state for an accessible account.

    Uses the OWNER's TradeLocker token (not the partner's — partners may
    have no broker connection at all). Verifies access first, then proxies
    one cheap broker call per response so a partner dashboard refresh
    doesn't hammer TL when many records are visible.
    """
    resolved = resolve_account_for_action(db, user, account_id, "view")
    owner = resolved.owner
    if not owner.tradelocker_token or not owner.tradelocker_account_id:
        raise HTTPException(
            502, "owner has no broker connection — cannot fetch snapshot"
        )

    async def _do_call(s: dict) -> dict:
        client = TradeLockerClient(env=s["env"])
        state = await client.get_account_state(
            resolved.account.tradelocker_account_id,
            s["token"],
            resolved.account.tradelocker_acc_num,
        )
        positions = await client.get_positions(
            resolved.account.tradelocker_account_id,
            s["token"],
            resolved.account.tradelocker_acc_num,
        )
        return {"state": state, "positions": positions}

    try:
        result = await call_with_refresh(owner.id, _do_call, db=db)
    except TradeLockerError as exc:
        raise HTTPException(502, f"broker error: {exc}")
    except ValueError:
        raise HTTPException(
            502, "owner has no broker session — cannot fetch snapshot"
        )

    return {
        "account_id": account_id,
        "tradelocker_account_id": resolved.account.tradelocker_account_id,
        "env": resolved.account.tradelocker_env,
        **result,
    }
