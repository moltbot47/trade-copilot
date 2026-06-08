"""TradingAccount + AccountAccessGrant management endpoints (DOM Phase A).

The model: a user owns one or more TradingAccount rows (currently each
mapped to a TradeLocker sub-account). The owner can grant other users
trader/viewer access via AccountAccessGrant, with optional per-grant
risk caps. DOM endpoints look up account_id through
`account_access.resolve_account_for_action` so an employee can act on
the owner's account without ever seeing the broker creds.

Endpoints
---------
GET    /api/accounts                       — list accounts I can see (owned + granted)
POST   /api/accounts                       — register a TradingAccount tied to my TL creds
DELETE /api/accounts/{id}                  — soft-deactivate (owner only)
GET    /api/accounts/{id}/grants           — list grants on an owned account
POST   /api/accounts/{id}/grants           — invite a grantee by email
DELETE /api/accounts/{id}/grants/{gid}     — revoke a grant
GET    /api/accounts/{id}/audit            — recent audit log for the account
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, EmailStr, Field
from sqlalchemy.orm import Session

from app.api.users import get_current_user
from app.core.account_access import list_accessible_accounts
from app.core.audit import record_audit
from app.db.database import get_db
from app.db.models import (
    AccountAccessGrant,
    AccountAccessRole,
    AuditLog,
    TradingAccount,
    User,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/accounts", tags=["accounts"])


def _client_ip(request: Optional[Request]) -> Optional[str]:
    """Extract the client IP, honoring X-Forwarded-For when present.

    Trade Copilot runs behind Fly/Vercel/CDN proxies; `request.client.host`
    will be the proxy IP. Prefer the leftmost entry in X-Forwarded-For
    when set (the originating client).
    """
    if request is None:
        return None
    xff = request.headers.get("x-forwarded-for")
    if xff:
        # Leftmost is the original client
        first = xff.split(",")[0].strip()
        if first:
            return first[:64]
    if request.client:
        return request.client.host[:64]
    return None


# ---------- Schemas ----------

class AccountOut(BaseModel):
    id: int
    label: str
    tradelocker_account_id: str
    env: str
    role: str
    owner_email: Optional[str] = None
    caps: Optional[dict] = None
    expires_at: Optional[str] = None


class AccountCreateIn(BaseModel):
    label: str = Field(..., min_length=1, max_length=120)
    # If omitted, use the caller's connected TL account (legacy path).
    tradelocker_account_id: Optional[str] = Field(None, max_length=64)
    tradelocker_acc_num: Optional[str] = Field(None, max_length=16)
    tradelocker_env: Optional[Literal["demo", "live"]] = None


class GrantIn(BaseModel):
    # Forbid unknown fields — prevents anyone from injecting server-derived
    # fields like granted_by_user_id or revoked_at through the POST body.
    model_config = ConfigDict(extra="forbid")

    grantee_email: EmailStr
    role: Literal["trader", "viewer"] = "trader"
    expires_at: Optional[datetime] = None
    max_lot_per_order: Optional[float] = Field(None, gt=0)
    max_daily_loss_usd: Optional[float] = Field(None, gt=0)
    allowed_instruments_csv: Optional[str] = Field(None, max_length=512)


class GrantOut(BaseModel):
    id: int
    account_id: int
    grantee_email: str
    role: str
    created_at: str
    expires_at: Optional[str]
    revoked_at: Optional[str]
    max_lot_per_order: Optional[float]
    max_daily_loss_usd: Optional[float]
    allowed_instruments_csv: Optional[str]


class AuditEntryOut(BaseModel):
    id: int
    ts: str
    actor_email: Optional[str]
    action: str
    details: str
    client_ip: Optional[str]


# ---------- Helpers ----------

def _owner_or_403(db: Session, account_id: int, user: User) -> TradingAccount:
    account = db.get(TradingAccount, account_id)
    if account is None or not account.is_active:
        raise HTTPException(status_code=404, detail="account not found")
    if account.owner_user_id != user.id:
        raise HTTPException(status_code=403, detail="only the owner can manage this account")
    return account


def _grant_to_out(g: AccountAccessGrant, grantee_email: str) -> GrantOut:
    return GrantOut(
        id=g.id,
        account_id=g.account_id,
        grantee_email=grantee_email,
        role=g.role.value,
        created_at=g.created_at.isoformat() if g.created_at else "",
        expires_at=g.expires_at.isoformat() if g.expires_at else None,
        revoked_at=g.revoked_at.isoformat() if g.revoked_at else None,
        max_lot_per_order=g.max_lot_per_order,
        max_daily_loss_usd=g.max_daily_loss_usd,
        allowed_instruments_csv=g.allowed_instruments_csv,
    )


# ---------- Endpoints ----------

@router.get("", response_model=list[AccountOut])
def list_accounts(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[AccountOut]:
    rows = list_accessible_accounts(db, user)
    return [AccountOut(**r) for r in rows]


@router.post("", response_model=AccountOut)
def register_account(
    payload: AccountCreateIn,
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> AccountOut:
    """Register a TradingAccount tied to the caller's connected TL credentials.

    For the initial migration path: if `tradelocker_account_id` is omitted,
    inherit the caller's `user.tradelocker_account_id`. This lets existing
    users one-click "promote" their already-connected account into the
    delegation model without re-entering broker creds.
    """
    tl_acct = (
        payload.tradelocker_account_id
        or user.tradelocker_account_id
    )
    if not tl_acct:
        raise HTTPException(
            status_code=400,
            detail="No TradeLocker account id. Connect a broker first or pass tradelocker_account_id.",
        )
    tl_num = payload.tradelocker_acc_num or user.tradelocker_acc_num or "1"
    env = payload.tradelocker_env or user.tradelocker_env or "demo"

    # Idempotent: don't double-register the same (owner, tl_account_id)
    existing = (
        db.query(TradingAccount)
        .filter_by(owner_user_id=user.id, tradelocker_account_id=tl_acct)
        .first()
    )
    if existing is not None:
        if not existing.is_active:
            existing.is_active = True
            db.commit()
        record_audit(
            db, user=user, action="trading_account_registered",
            details={"account_id": existing.id, "reactivated": True},
            account_id=existing.id, client_ip=_client_ip(request),
        )
        db.commit()
        return AccountOut(
            id=existing.id,
            label=existing.label or f"account #{existing.tradelocker_account_id}",
            tradelocker_account_id=existing.tradelocker_account_id,
            env=existing.tradelocker_env,
            role="owner",
            owner_email=user.email,
            caps=None,
        )

    row = TradingAccount(
        owner_user_id=user.id,
        label=payload.label,
        tradelocker_account_id=tl_acct,
        tradelocker_acc_num=tl_num,
        tradelocker_env=env,
        is_active=True,
    )
    db.add(row)
    db.flush()  # populate row.id
    record_audit(
        db, user=user, action="trading_account_registered",
        details={"account_id": row.id, "label": payload.label, "env": env},
        account_id=row.id, client_ip=_client_ip(request),
    )
    db.commit()
    return AccountOut(
        id=row.id,
        label=row.label,
        tradelocker_account_id=row.tradelocker_account_id,
        env=row.tradelocker_env,
        role="owner",
        owner_email=user.email,
        caps=None,
    )


@router.delete("/{account_id}")
def deactivate_account(
    account_id: int,
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    account = _owner_or_403(db, account_id, user)
    account.is_active = False
    # Revoke all outstanding grants — a deactivated account has no traders.
    now = datetime.utcnow()
    grants = (
        db.query(AccountAccessGrant)
        .filter_by(account_id=account.id, revoked_at=None)
        .all()
    )
    for g in grants:
        g.revoked_at = now
    record_audit(
        db, user=user, action="trading_account_removed",
        details={"account_id": account.id, "revoked_grants": len(grants)},
        account_id=account.id, client_ip=_client_ip(request),
    )
    db.commit()
    return {"status": "ok", "revoked_grants": len(grants)}


@router.get("/{account_id}/grants", response_model=list[GrantOut])
def list_grants(
    account_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[GrantOut]:
    _owner_or_403(db, account_id, user)
    grants = (
        db.query(AccountAccessGrant)
        .filter_by(account_id=account_id)
        .order_by(AccountAccessGrant.created_at.desc())
        .all()
    )
    out: list[GrantOut] = []
    for g in grants:
        grantee = db.get(User, g.grantee_user_id)
        out.append(_grant_to_out(g, grantee.email if grantee else "?"))
    return out


@router.post("/{account_id}/grants", response_model=GrantOut)
def create_grant(
    account_id: int,
    payload: GrantIn,
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> GrantOut:
    account = _owner_or_403(db, account_id, user)

    grantee = db.query(User).filter_by(email=str(payload.grantee_email).lower()).first()
    if grantee is None:
        raise HTTPException(
            status_code=404,
            detail=f"no user found with email {payload.grantee_email}. They must register and log in once first.",
        )
    if grantee.id == user.id:
        raise HTTPException(status_code=400, detail="cannot grant access to yourself (you are the owner)")

    # If a truly-active grant exists, refuse. "Active" matches _active_grant's
    # definition: not revoked AND (no expiry OR expiry in future). A merely-
    # expired non-revoked row should NOT block a fresh grant.
    now = datetime.utcnow()
    rows = (
        db.query(AccountAccessGrant)
        .filter_by(
            account_id=account.id,
            grantee_user_id=grantee.id,
            revoked_at=None,
        )
        .all()
    )
    existing = next(
        (r for r in rows if r.expires_at is None or r.expires_at > now),
        None,
    )
    if existing is not None:
        raise HTTPException(
            status_code=409,
            detail=f"active grant already exists for {payload.grantee_email}. Revoke it first to issue a new one.",
        )

    role_enum = (
        AccountAccessRole.trader if payload.role == "trader" else AccountAccessRole.viewer
    )
    g = AccountAccessGrant(
        account_id=account.id,
        grantee_user_id=grantee.id,
        role=role_enum,
        granted_by_user_id=user.id,
        expires_at=payload.expires_at,
        max_lot_per_order=payload.max_lot_per_order,
        max_daily_loss_usd=payload.max_daily_loss_usd,
        allowed_instruments_csv=payload.allowed_instruments_csv,
    )
    db.add(g)
    db.flush()
    record_audit(
        db, user=user, action="access_grant_created",
        details={
            "grant_id": g.id,
            "grantee_email": grantee.email,
            "role": role_enum.value,
            "caps": {
                "max_lot_per_order": payload.max_lot_per_order,
                "max_daily_loss_usd": payload.max_daily_loss_usd,
                "allowed_instruments_csv": payload.allowed_instruments_csv,
            },
            "expires_at": payload.expires_at.isoformat() if payload.expires_at else None,
        },
        account_id=account.id, client_ip=_client_ip(request),
    )
    db.commit()
    return _grant_to_out(g, grantee.email)


@router.delete("/{account_id}/grants/{grant_id}")
def revoke_grant(
    account_id: int,
    grant_id: int,
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    account = _owner_or_403(db, account_id, user)
    g = db.get(AccountAccessGrant, grant_id)
    if g is None or g.account_id != account.id:
        raise HTTPException(status_code=404, detail="grant not found")
    if g.revoked_at is not None:
        return {"status": "already_revoked"}
    g.revoked_at = datetime.utcnow()
    grantee = db.get(User, g.grantee_user_id)
    record_audit(
        db, user=user, action="access_grant_revoked",
        details={
            "grant_id": g.id,
            "grantee_email": grantee.email if grantee else "?",
        },
        account_id=account.id, client_ip=_client_ip(request),
    )
    db.commit()
    return {"status": "revoked"}


@router.get("/{account_id}/audit", response_model=list[AuditEntryOut])
def account_audit(
    account_id: int,
    limit: int = 100,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[AuditEntryOut]:
    """Recent audit log for an owned account.

    Owner-only — employees don't get to see the full audit trail (they
    can see their own actions in their own session log).
    """
    _owner_or_403(db, account_id, user)
    limit = max(1, min(500, limit))
    rows = (
        db.query(AuditLog)
        .filter(AuditLog.account_id == account_id)
        .order_by(AuditLog.ts.desc())
        .limit(limit)
        .all()
    )
    return [
        AuditEntryOut(
            id=r.id,
            ts=r.ts.isoformat() if r.ts else "",
            actor_email=r.actor_email,
            action=r.action,
            details=r.details,
            client_ip=r.client_ip,
        )
        for r in rows
    ]
