"""DOM (Depth of Market) endpoints — phase A delegation + phase B trading actions.

The flow:
  - Every endpoint takes an `account_id` (the TradingAccount the actor wants
    to act on). The caller must be the owner OR have an active grant.
  - Broker credentials come from the OWNER's user row, never the actor's.
    Actor identity is preserved for the audit log and risk-cap enforcement.
  - Audit log records `actor_email` + `account_id` on every order/position
    event so an owner can answer "what did my employee do on my account
    today" without ambiguity.

Endpoints
---------
POST   /api/dom/order                                — place order on account
POST   /api/dom/flatten                              — close all positions
GET    /api/dom/state                                — balance + positions + working orders
DELETE /api/dom/positions/{pid}                      — close one position
PATCH  /api/dom/positions/{pid}                      — modify SL/TP on one position
DELETE /api/dom/orders/{oid}                         — cancel one working order
GET    /api/dom/daily-pnl                            — today's realized P&L (for cap UI)
"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from datetime import datetime, time as dt_time
from typing import Any, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from app.api.users import get_current_user
from app.core.account_access import (
    ResolvedAccount,
    check_grant_caps,
    resolve_account_for_action,
)
from app.core.audit import record_audit
from app.core.crypto import decrypt
from app.core.tradelocker_client import TradeLockerClient, TradeLockerError
from app.db.database import get_db
from app.db.models import AccountAccessGrant, AccountAccessRole, AuditLog, User

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/dom", tags=["dom"])


# ---------- Schemas ----------

class DomOrderIn(BaseModel):
    # Reject unknown fields so a malicious POST can't smuggle in server-
    # derived data (e.g., fake account_id under a typo'd field name).
    model_config = ConfigDict(extra="forbid")

    account_id: int = Field(..., description="TradingAccount.id to act on")
    instrument: str = Field(
        ...,
        min_length=1,
        max_length=16,
        pattern=r"^[A-Za-z0-9_.]{1,16}$",
        description="A-Z/0-9 only — broker symbols are short and clean",
    )
    side: Literal["buy", "sell"]
    qty: float = Field(..., gt=0, le=1000.0)
    order_type: Literal["market", "limit", "stop"] = "market"
    limit_price: Optional[float] = Field(None, gt=0)
    stop_price: Optional[float] = Field(None, gt=0)
    sl: Optional[float] = Field(None, gt=0)
    tp: Optional[float] = Field(None, gt=0)
    fan_to_followers: bool = Field(False, description="P1.5 stub")


class DomOrderOut(BaseModel):
    signal_id: str
    master_order_id: Optional[str]
    master_status: Literal["filled", "rejected", "error", "not_connected", "blocked"]
    master_error: Optional[str] = None
    followers_dispatched: int
    followers_acknowledged: int
    elapsed_ms: int


class DomStateOut(BaseModel):
    account_id: int
    label: str
    env: str
    instrument: str
    balance: float
    equity: float
    available_funds: float
    open_pnl: float
    positions: list[dict]
    working_orders: list[dict]
    role: str
    caps: Optional[dict] = None
    daily_realized_pnl_usd: float = 0.0


class FlattenIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    account_id: int


class FlattenOut(BaseModel):
    closed: int
    errors: list[str]
    elapsed_ms: int


class ModifyPositionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    account_id: int
    stop_loss: Optional[float] = Field(None, gt=0)
    take_profit: Optional[float] = Field(None, gt=0)


class CancelOrderIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    account_id: int


# ---------- Helpers ----------

def _new_signal_id() -> str:
    return f"dom-{uuid.uuid4().hex[:12]}"


def _utc_today_start() -> datetime:
    """UTC midnight today — daily-loss buckets are UTC days."""
    return datetime.combine(datetime.utcnow().date(), dt_time.min)


def _daily_realized_pnl_for_actor(
    db: Session, account_id: int, actor: User
) -> float:
    """Sum today's realized P&L attributable to `actor` on `account_id`.

    Reads from AuditLog rows where action='manual_position_close' or
    'manual_flatten_all' with details containing realized_pnl_usd. Cheap
    O(n_today) for a small audit log; if scale demands, add a daily
    rollup table later.
    """
    import json
    start = _utc_today_start()
    rows = (
        db.query(AuditLog)
        .filter(
            AuditLog.account_id == account_id,
            AuditLog.user_id == actor.id,
            AuditLog.ts >= start,
            AuditLog.action.in_(("manual_position_close", "manual_flatten_all")),
        )
        .all()
    )
    total = 0.0
    for r in rows:
        try:
            d = json.loads(r.details or "{}")
            total += float(d.get("realized_pnl_usd") or 0.0)
        except Exception:
            continue
    return total


def _resolve_or_403(
    db: Session, actor: User, account_id: int, mode: str
) -> ResolvedAccount:
    """Wrap resolver — converts to clear HTTP errors. mode in {'trade','view'}."""
    return resolve_account_for_action(db, actor, account_id, mode)  # type: ignore[arg-type]


def _owner_token(owner: User) -> str:
    if not owner.tradelocker_token:
        raise HTTPException(status_code=400, detail="account owner has no broker token (re-auth needed)")
    token = decrypt(owner.tradelocker_token)
    if not token:
        raise HTTPException(status_code=400, detail="broker token unreadable")
    return token


def _client(env: str) -> TradeLockerClient:
    return TradeLockerClient(env=env or "demo")


def _client_ip(request: Optional[Request]) -> Optional[str]:
    """Originating client IP, honoring X-Forwarded-For when behind a proxy.

    Fly + Vercel both inject XFF; `request.client.host` alone returns the
    proxy address which is useless for forensics.
    """
    if request is None:
        return None
    xff = request.headers.get("x-forwarded-for")
    if xff:
        first = xff.split(",")[0].strip()
        if first:
            return first[:64]
    if request.client:
        return request.client.host[:64]
    return None


async def _ensure_position_belongs_to_account(
    client: TradeLockerClient,
    account_tl_id: str,
    token: str,
    acc_num: str,
    position_id: str,
) -> dict:
    """Verify `position_id` is a real position on the broker account.

    Closes the cross-account position-hijack: without this, a trader-grant
    on account A could pass a position_id from account B and the broker
    would honour the DELETE (TL doesn't validate position ownership against
    the auth token — the token IS the only identity).
    """
    try:
        positions = await client.get_positions(account_tl_id, token, acc_num)
    except TradeLockerError as exc:
        raise HTTPException(status_code=502, detail=f"broker error: {exc}")
    for p in positions:
        if str(p.get("id")) == str(position_id):
            return p
    raise HTTPException(
        status_code=404,
        detail="position not found on this account",
    )


async def _ensure_order_belongs_to_account(
    client: TradeLockerClient,
    account_tl_id: str,
    token: str,
    acc_num: str,
    order_id: str,
) -> None:
    """Verify `order_id` is a real working order on the broker account."""
    try:
        rows = await client.get_orders(account_tl_id, token, acc_num)
    except TradeLockerError as exc:
        raise HTTPException(status_code=502, detail=f"broker error: {exc}")
    for row in rows:
        rid = row[0] if isinstance(row, list) and row else (
            row.get("id") if isinstance(row, dict) else None
        )
        if rid is not None and str(rid) == str(order_id):
            return
    raise HTTPException(
        status_code=404,
        detail="order not found on this account",
    )


# ---------- Endpoints ----------

@router.post("/order", response_model=DomOrderOut)
async def submit_order(
    payload: DomOrderIn,
    request: Request,
    actor: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> DomOrderOut:
    """Place a market/limit/stop order on the target TradingAccount.

    Authorization: actor must own the account or hold an active trader grant.
    Risk caps: if a grant is in effect, per-order lot, instrument whitelist,
    and daily realized loss are enforced server-side before the broker call.
    """
    resolved = _resolve_or_403(db, actor, payload.account_id, "trade")
    account = resolved.account

    # Risk-cap enforcement
    if resolved.grant is not None:
        reason = check_grant_caps(
            resolved.grant, instrument=payload.instrument, qty=payload.qty
        )
        if reason is not None:
            record_audit(
                db, user=actor, action="access_grant_blocked",
                details={"reason": reason, "instrument": payload.instrument, "qty": payload.qty},
                account_id=account.id, client_ip=_client_ip(request),
            )
            db.commit()
            return DomOrderOut(
                signal_id=_new_signal_id(),
                master_order_id=None,
                master_status="blocked",
                master_error=reason,
                followers_dispatched=0,
                followers_acknowledged=0,
                elapsed_ms=0,
            )
        # Daily loss cap (consult audit log)
        if resolved.grant.max_daily_loss_usd is not None:
            today_pnl = _daily_realized_pnl_for_actor(db, account.id, actor)
            cap = -float(resolved.grant.max_daily_loss_usd)  # loss as negative
            if today_pnl <= cap:
                reason = (
                    f"daily loss cap reached: ${today_pnl:.2f} realized "
                    f"vs cap ${resolved.grant.max_daily_loss_usd:.2f}"
                )
                record_audit(
                    db, user=actor, action="access_grant_blocked",
                    details={"reason": reason, "today_pnl": today_pnl},
                    account_id=account.id, client_ip=_client_ip(request),
                )
                db.commit()
                return DomOrderOut(
                    signal_id=_new_signal_id(),
                    master_order_id=None,
                    master_status="blocked",
                    master_error=reason,
                    followers_dispatched=0,
                    followers_acknowledged=0,
                    elapsed_ms=0,
                )

    signal_id = _new_signal_id()
    t0 = time.monotonic()
    owner = resolved.owner
    token = _owner_token(owner)
    client = _client(account.tradelocker_env)

    master_id: Optional[str] = None
    master_status: str = "error"
    master_err: Optional[str] = None
    try:
        result = await client.place_order(
            account_id=account.tradelocker_account_id,
            token=token,
            acc_num=account.tradelocker_acc_num or "1",
            symbol=payload.instrument,
            side=payload.side,
            qty=payload.qty,
            sl=payload.sl,
            tp=payload.tp,
            order_type=payload.order_type,
            client_order_id=signal_id,
        )
        master_id = str(result.get("order_id") or "")
        master_status = "filled"
    except TradeLockerError as exc:
        msg = str(exc)
        master_status = "rejected" if "rejected" in msg.lower() else "error"
        master_err = msg

    elapsed_ms = int((time.monotonic() - t0) * 1000)
    record_audit(
        db,
        user=actor,
        action="order_placed" if master_status == "filled" else "order_rejected",
        details={
            "signal_id": signal_id,
            "instrument": payload.instrument,
            "side": payload.side,
            "qty": payload.qty,
            "order_type": payload.order_type,
            "sl": payload.sl,
            "tp": payload.tp,
            "broker_order_id": master_id,
            "error": master_err,
            "elapsed_ms": elapsed_ms,
        },
        account_id=account.id, client_ip=_client_ip(request),
    )
    db.commit()
    logger.info(
        "dom.order signal=%s actor=%s account=%s status=%s elapsed=%dms",
        signal_id, actor.id, account.id, master_status, elapsed_ms,
    )
    return DomOrderOut(
        signal_id=signal_id,
        master_order_id=master_id or None,
        master_status=master_status,  # type: ignore[arg-type]
        master_error=master_err,
        followers_dispatched=0,
        followers_acknowledged=0,
        elapsed_ms=elapsed_ms,
    )


@router.post("/flatten", response_model=FlattenOut)
async def flatten_all(
    payload: FlattenIn,
    request: Request,
    actor: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> FlattenOut:
    resolved = _resolve_or_403(db, actor, payload.account_id, "trade")
    account = resolved.account
    token = _owner_token(resolved.owner)
    client = _client(account.tradelocker_env)
    acc_num = account.tradelocker_acc_num or "1"

    t0 = time.monotonic()
    try:
        positions = await client.get_positions(account.tradelocker_account_id, token, acc_num)
    except TradeLockerError as exc:
        raise HTTPException(status_code=502, detail=f"broker error: {exc}")

    # Daily-loss cap pre-check: closing right now would realize the sum of
    # all open unrealizedPl. If that pushes the actor past the cap, refuse.
    # (Caps don't apply to the owner.)
    if resolved.grant is not None and resolved.grant.max_daily_loss_usd is not None:
        snapshot_pnl = 0.0
        for pos in positions:
            try:
                snapshot_pnl += float(pos.get("unrealizedPl") or 0.0)
            except Exception:
                pass
        today_pnl = _daily_realized_pnl_for_actor(db, account.id, actor)
        projected = today_pnl + snapshot_pnl
        cap = -float(resolved.grant.max_daily_loss_usd)
        if projected <= cap:
            reason = (
                f"flatten would breach daily loss cap: projected ${projected:.2f} "
                f"(today ${today_pnl:.2f} + snapshot ${snapshot_pnl:.2f}) "
                f"vs cap ${resolved.grant.max_daily_loss_usd:.2f}. "
                f"Owner can flatten — ask them."
            )
            record_audit(
                db, user=actor, action="access_grant_blocked",
                details={"reason": reason, "context": "flatten"},
                account_id=account.id, client_ip=_client_ip(request),
            )
            db.commit()
            raise HTTPException(status_code=403, detail=reason)

    closed = 0
    errors: list[str] = []
    realized_pnl = 0.0
    for pos in positions:
        pid = pos.get("id")
        if not pid:
            continue
        try:
            await client.close_position(str(pid), token, acc_num)
            closed += 1
            # Track unrealized at close-time as a proxy for realized — actual
            # broker realization confirmation is asynchronous; this is the
            # best snapshot available without a fills feed.
            try:
                realized_pnl += float(pos.get("unrealizedPl") or 0.0)
            except Exception:
                pass
        except TradeLockerError as exc:
            errors.append(f"position {pid}: {exc}")

    elapsed_ms = int((time.monotonic() - t0) * 1000)
    record_audit(
        db, user=actor, action="manual_flatten_all",
        details={
            "closed": closed,
            "errors_count": len(errors),
            "realized_pnl_usd": realized_pnl,
            "elapsed_ms": elapsed_ms,
        },
        account_id=account.id, client_ip=_client_ip(request),
    )
    db.commit()
    return FlattenOut(closed=closed, errors=errors, elapsed_ms=elapsed_ms)


@router.get("/state", response_model=DomStateOut)
async def get_state(
    account_id: int = Query(..., description="TradingAccount.id"),
    instrument: str = "NAS100",
    actor: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> DomStateOut:
    resolved = _resolve_or_403(db, actor, account_id, "view")
    account = resolved.account
    token = _owner_token(resolved.owner)
    client = _client(account.tradelocker_env)
    acc_num = account.tradelocker_acc_num or "1"

    balance = equity = available = open_pnl = 0.0
    positions: list[dict] = []
    working: list[dict] = []

    try:
        state = await client.get_account_state(account.tradelocker_account_id, token, acc_num)
        balance = float(state.get("balance", 0) or 0)
        equity = float(state.get("projectedBalance", balance) or balance)
        available = float(state.get("availableFunds", balance) or balance)
        open_pnl = float(state.get("openNetPnL", 0) or 0)
    except TradeLockerError as exc:
        logger.info("dom.state account fetch failed: %s", exc)

    try:
        raw_positions = await client.get_positions(account.tradelocker_account_id, token, acc_num)
        for p in raw_positions:
            positions.append({
                "id": str(p.get("id")) if p.get("id") is not None else None,
                "side": p.get("side"),
                "qty": p.get("qty"),
                "avg_price": p.get("avgPrice"),
                "unrealized_pl": p.get("unrealizedPl"),
                "stop_loss_id": p.get("stopLossId"),
                "take_profit_id": p.get("takeProfitId"),
                "tradable_instrument_id": p.get("tradableInstrumentId"),
            })
    except TradeLockerError as exc:
        logger.info("dom.state positions fetch failed: %s", exc)

    try:
        raw_orders = await client.get_orders(account.tradelocker_account_id, token, acc_num)
        # Defensive: orders are array-of-arrays on TL; we just surface as-is
        # for now so the UI can list/cancel by id. Drop rows with null id —
        # surfacing "None" as a string id would let the UI fire DELETE on
        # /trade/orders/None, which either 404s or matches an order whose
        # actual id happens to be the string "None".
        for row in raw_orders:
            rid: Any = None
            if isinstance(row, list) and len(row) > 0:
                rid = row[0]
            elif isinstance(row, dict):
                rid = row.get("id")
            if rid is None:
                continue
            working.append({"id": str(rid), "raw": row})
    except TradeLockerError as exc:
        logger.info("dom.state orders fetch failed: %s", exc)

    daily_pnl = _daily_realized_pnl_for_actor(db, account.id, actor)

    caps = None
    # Only expose caps to the actor whose caps they actually are — a viewer
    # with a different grant shouldn't see another grantee's loss cap.
    if (
        resolved.grant is not None
        and resolved.grant.role == AccountAccessRole.trader
    ):
        caps = {
            "max_lot_per_order": resolved.grant.max_lot_per_order,
            "max_daily_loss_usd": resolved.grant.max_daily_loss_usd,
            "allowed_instruments_csv": resolved.grant.allowed_instruments_csv,
        }

    return DomStateOut(
        account_id=account.id,
        label=account.label or f"account #{account.tradelocker_account_id}",
        env=account.tradelocker_env,
        instrument=instrument,
        balance=balance,
        equity=equity,
        available_funds=available,
        open_pnl=open_pnl,
        positions=positions,
        working_orders=working,
        role=resolved.role.value,
        caps=caps,
        daily_realized_pnl_usd=daily_pnl,
    )


@router.delete("/positions/{position_id}")
async def close_one_position(
    position_id: str,
    request: Request,
    account_id: int = Query(...),
    actor: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    resolved = _resolve_or_403(db, actor, account_id, "trade")
    account = resolved.account
    token = _owner_token(resolved.owner)
    client = _client(account.tradelocker_env)
    acc_num = account.tradelocker_acc_num or "1"

    # Verify the position actually lives on this account. Without this, a
    # grantee on account A could pass a position_id from account B and the
    # broker would honour the DELETE (TL auth is by token, no ownership
    # check at the broker side).
    pos = await _ensure_position_belongs_to_account(
        client, account.tradelocker_account_id, token, acc_num, position_id
    )
    try:
        realized_pnl = float(pos.get("unrealizedPl") or 0.0)
    except Exception:
        realized_pnl = 0.0

    try:
        await client.close_position(position_id, token, acc_num)
    except TradeLockerError as exc:
        record_audit(
            db, user=actor, action="manual_position_close",
            details={"position_id": position_id, "error": str(exc)},
            account_id=account.id, client_ip=_client_ip(request),
        )
        db.commit()
        raise HTTPException(status_code=502, detail=f"close failed: {exc}")

    record_audit(
        db, user=actor, action="manual_position_close",
        details={
            "position_id": position_id,
            "realized_pnl_usd": realized_pnl,
        },
        account_id=account.id, client_ip=_client_ip(request),
    )
    db.commit()
    return {"status": "closed", "position_id": position_id, "realized_pnl_usd": realized_pnl}


@router.patch("/positions/{position_id}")
async def modify_position_sl_tp(
    position_id: str,
    payload: ModifyPositionIn,
    request: Request,
    actor: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    if payload.stop_loss is None and payload.take_profit is None:
        raise HTTPException(status_code=400, detail="must provide stop_loss and/or take_profit")
    resolved = _resolve_or_403(db, actor, payload.account_id, "trade")
    account = resolved.account
    token = _owner_token(resolved.owner)
    client = _client(account.tradelocker_env)
    acc_num = account.tradelocker_acc_num or "1"

    # Cross-account hijack guard — see close_one_position for rationale.
    await _ensure_position_belongs_to_account(
        client, account.tradelocker_account_id, token, acc_num, position_id
    )

    try:
        await client.modify_position(
            position_id, token, acc_num,
            stop_loss=payload.stop_loss,
            take_profit=payload.take_profit,
        )
    except TradeLockerError as exc:
        record_audit(
            db, user=actor, action="manual_position_modify",
            details={"position_id": position_id, "error": str(exc)},
            account_id=account.id, client_ip=_client_ip(request),
        )
        db.commit()
        raise HTTPException(status_code=502, detail=f"modify failed: {exc}")

    record_audit(
        db, user=actor, action="manual_position_modify",
        details={
            "position_id": position_id,
            "stop_loss": payload.stop_loss,
            "take_profit": payload.take_profit,
        },
        account_id=account.id, client_ip=_client_ip(request),
    )
    db.commit()
    return {"status": "modified", "position_id": position_id}


@router.delete("/orders/{order_id}")
async def cancel_working_order(
    order_id: str,
    request: Request,
    account_id: int = Query(...),
    actor: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    resolved = _resolve_or_403(db, actor, account_id, "trade")
    account = resolved.account
    token = _owner_token(resolved.owner)
    client = _client(account.tradelocker_env)
    acc_num = account.tradelocker_acc_num or "1"

    # Cross-account hijack guard — verify the order exists on this account.
    await _ensure_order_belongs_to_account(
        client, account.tradelocker_account_id, token, acc_num, order_id
    )

    try:
        await client.cancel_order(order_id, token, acc_num)
    except TradeLockerError as exc:
        record_audit(
            db, user=actor, action="manual_order_cancel",
            details={"order_id": order_id, "error": str(exc)},
            account_id=account.id, client_ip=_client_ip(request),
        )
        db.commit()
        raise HTTPException(status_code=502, detail=f"cancel failed: {exc}")

    record_audit(
        db, user=actor, action="manual_order_cancel",
        details={"order_id": order_id},
        account_id=account.id, client_ip=_client_ip(request),
    )
    db.commit()
    return {"status": "cancelled", "order_id": order_id}


@router.get("/daily-pnl")
def daily_pnl(
    account_id: int = Query(...),
    actor: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    resolved = _resolve_or_403(db, actor, account_id, "view")
    pnl = _daily_realized_pnl_for_actor(db, resolved.account.id, actor)
    cap = (
        float(resolved.grant.max_daily_loss_usd)
        if resolved.grant and resolved.grant.max_daily_loss_usd is not None
        else None
    )
    return {
        "account_id": account_id,
        "today_realized_pnl_usd": pnl,
        "max_daily_loss_usd": cap,
        "remaining_loss_room_usd": (cap + pnl) if cap is not None else None,
    }
