"""Account access-control: who may trade / view which TradingAccount.

Single source of truth for the delegation model from Phase A. DOM endpoints
must go through `resolve_account_for_action` instead of reading
`user.tradelocker_*` directly — that ensures an employee acting on the
owner's account gets the OWNER's broker credentials but the actor's
identity is preserved for audit and risk-cap checks.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Optional

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.db.models import (
    AccountAccessGrant,
    AccountAccessRole,
    TradingAccount,
    User,
)


Mode = Literal["trade", "view"]


@dataclass
class ResolvedAccount:
    """Bundle returned to DOM endpoints: the broker creds + the grant in effect.

    `grant` is None when the actor IS the owner — owner access is implicit
    and has no caps beyond the owner's own user-level limits. When `grant`
    is set, the DOM endpoint must consult `grant.max_lot_per_order`,
    `grant.max_daily_loss_usd`, and `grant.allowed_instruments_csv`.
    """
    account: TradingAccount
    owner: User
    grant: Optional[AccountAccessGrant]
    role: AccountAccessRole


def _active_grant(
    db: Session, account_id: int, user_id: int
) -> Optional[AccountAccessGrant]:
    """Return a non-revoked, non-expired grant for (account, user), or None.

    If multiple matching rows somehow exist (DB-level shenanigans, since
    accounts.create_grant prevents the second insert), return the most
    recently created one — deterministic and least surprising.
    """
    now = datetime.utcnow()
    q = (
        db.query(AccountAccessGrant)
        .filter(
            AccountAccessGrant.account_id == account_id,
            AccountAccessGrant.grantee_user_id == user_id,
            AccountAccessGrant.revoked_at.is_(None),
        )
        .order_by(AccountAccessGrant.created_at.desc())
    )
    for g in q.all():
        if g.expires_at is None or g.expires_at > now:
            return g
    return None


def may_view(db: Session, user: User, account: TradingAccount) -> bool:
    if account.owner_user_id == user.id:
        return True
    return _active_grant(db, account.id, user.id) is not None


def may_trade(db: Session, user: User, account: TradingAccount) -> bool:
    if account.owner_user_id == user.id:
        return True
    g = _active_grant(db, account.id, user.id)
    return g is not None and g.role == AccountAccessRole.trader


def resolve_account_for_action(
    db: Session, actor: User, account_id: int, mode: Mode
) -> ResolvedAccount:
    """Lookup helper for DOM endpoints. Raises 404 on every failure.

    Why a single 404 for both "doesn't exist" and "no access"?
      Account IDs autoincrement. A 403/404 differential lets any
      authenticated user enumerate which account ids exist by probing
      ID=1..N. Collapsing the two responses closes that information leak.
    """
    NOT_FOUND = HTTPException(status_code=404, detail="account not found")

    account = db.get(TradingAccount, account_id)
    if account is None or not account.is_active:
        raise NOT_FOUND

    owner = db.get(User, account.owner_user_id)
    if owner is None:
        raise NOT_FOUND

    if mode == "trade":
        if account.owner_user_id == actor.id:
            return ResolvedAccount(
                account=account, owner=owner, grant=None,
                role=AccountAccessRole.trader,
            )
        g = _active_grant(db, account.id, actor.id)
        if g is None or g.role != AccountAccessRole.trader:
            raise NOT_FOUND
        return ResolvedAccount(
            account=account, owner=owner, grant=g, role=g.role,
        )

    # view mode
    if account.owner_user_id == actor.id:
        return ResolvedAccount(
            account=account, owner=owner, grant=None,
            role=AccountAccessRole.trader,
        )
    g = _active_grant(db, account.id, actor.id)
    if g is None:
        raise NOT_FOUND
    return ResolvedAccount(account=account, owner=owner, grant=g, role=g.role)


def list_accessible_accounts(db: Session, user: User) -> list[dict]:
    """Return every account `user` can see — owned + granted, with role label.

    Used by the account selector dropdown on the DOM page.
    """
    out: list[dict] = []

    owned = (
        db.query(TradingAccount)
        .filter(
            TradingAccount.owner_user_id == user.id,
            TradingAccount.is_active.is_(True),
        )
        .all()
    )
    for a in owned:
        out.append({
            "id": a.id,
            "label": a.label or f"account #{a.tradelocker_account_id}",
            "tradelocker_account_id": a.tradelocker_account_id,
            "env": a.tradelocker_env,
            "role": "owner",
            "owner_email": user.email,
            "caps": None,
        })

    now = datetime.utcnow()
    grants = (
        db.query(AccountAccessGrant)
        .filter(
            AccountAccessGrant.grantee_user_id == user.id,
            AccountAccessGrant.revoked_at.is_(None),
        )
        .all()
    )
    for g in grants:
        if g.expires_at is not None and g.expires_at <= now:
            continue
        a = db.get(TradingAccount, g.account_id)
        if a is None or not a.is_active:
            continue
        owner = db.get(User, a.owner_user_id)
        out.append({
            "id": a.id,
            "label": a.label or f"account #{a.tradelocker_account_id}",
            "tradelocker_account_id": a.tradelocker_account_id,
            "env": a.tradelocker_env,
            "role": g.role.value,
            "owner_email": owner.email if owner else None,
            "caps": {
                "max_lot_per_order": g.max_lot_per_order,
                "max_daily_loss_usd": g.max_daily_loss_usd,
                "allowed_instruments_csv": g.allowed_instruments_csv,
            },
            "expires_at": g.expires_at.isoformat() if g.expires_at else None,
        })

    return out


def check_grant_caps(
    grant: AccountAccessGrant,
    *,
    instrument: str,
    qty: float,
) -> Optional[str]:
    """Return None if the order is allowed under this grant's caps, else a
    short human-readable reason string.

    Daily-loss enforcement requires consulting today's realized P&L for
    this (account, actor) pair — see `daily_realized_pnl_for_grant` in the
    DOM endpoint (not here, to keep this module DB-free for unit testing).
    """
    if grant.max_lot_per_order is not None and qty > float(grant.max_lot_per_order):
        return (
            f"order qty {qty} exceeds per-order cap "
            f"{grant.max_lot_per_order} for your grant"
        )
    if grant.allowed_instruments_csv:
        allowed = {
            s.strip().upper()
            for s in grant.allowed_instruments_csv.split(",")
            if s.strip()
        }
        if instrument.upper() not in allowed:
            return (
                f"instrument {instrument} not in grant whitelist "
                f"({sorted(allowed)})"
            )
    return None
