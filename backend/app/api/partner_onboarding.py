"""Partner self-serve onboarding — invite links, uploads, owner approval.

Flow
----
1. Owner creates an invite      → POST /api/partner-invites
2. Owner sends /invite/<token>  → partner opens it (public, no login)
3. Partner submits a strategy   → POST /api/invite/<token>/submit
     - "source": uploads a .py (AST-scanned before it's even stored)
     - "http":   pastes an endpoint URL + HMAC secret (secret encrypted)
4. Owner reviews + approves      → POST /api/partner-submissions/<id>/approve
     - resolves/creates the partner User by email
     - issues a scoped, time-boxed **viewer** AccountAccessGrant
     - creates a partner Bot (strategy_type=partner, dispatched by slug)
     - registers the strategy in the runtime registry (exec/http-proxy)
     - seeds StrategyState.config_json with the partner's params
   …then the owner binds the bot to a demo account and starts it via the
   isolation harness. Nothing the partner uploaded runs before approval.

Security boundary: every owner endpoint requires get_current_user; the
public invite routes are token-gated and single-use. A source upload is
held — never imported — until the AST scan passes AND the owner approves.
"""
from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime
from typing import Optional

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Request,
    UploadFile,
)
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy.orm import Session

from app.api.accounts import _client_ip, _owner_or_403
from app.api.users import get_current_user, get_or_create_user
from app.core.audit import record_audit
from app.core.crypto import encrypt
from app.core.strategy_validator import validate_strategy_source
from app.db.database import SessionLocal, get_db
from app.db.models import (
    AccountAccessGrant,
    AccountAccessRole,
    Bot,
    PartnerInvite,
    PartnerSubmission,
    StrategyAccount,
    StrategyState,
    StrategyType,
    TradingAccount,
    User,
)
from app.strategies import partner_loader

logger = logging.getLogger(__name__)
router = APIRouter(tags=["partner-onboarding"])

# Hard ceiling for an uploaded source file (bytes). The AST scanner has its
# own char ceiling; this stops us reading a huge body into memory first.
_MAX_UPLOAD_BYTES = 256 * 1024


# --------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------- #
def _slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9_]+", "_", (value or "").strip().lower()).strip("_")
    return (slug or "strategy")[:48]


def _unique_slug(db: Session, base: str) -> str:
    """A slug not used by any Bot or non-rejected PartnerSubmission."""
    base = base or "strategy"
    slug = base
    i = 2
    while (
        db.query(Bot).filter(Bot.slug == slug).first() is not None
        or db.query(PartnerSubmission)
        .filter(
            PartnerSubmission.strategy_slug == slug,
            PartnerSubmission.status != "rejected",
        )
        .first()
        is not None
    ):
        slug = f"{base[:44]}_{i}"
        i += 1
    return slug


def _invite_state(inv: PartnerInvite) -> str:
    now = datetime.utcnow()
    if inv.revoked_at is not None:
        return "revoked"
    if inv.used_at is not None:
        return "used"
    if inv.expires_at is not None and inv.expires_at <= now:
        return "expired"
    return "active"


def _resolve_active_invite(db: Session, token: str) -> PartnerInvite:
    inv = db.query(PartnerInvite).filter(PartnerInvite.token == token).first()
    if inv is None:
        raise HTTPException(404, "invite not found")
    state = _invite_state(inv)
    if state != "active":
        # 410 Gone — the link existed but is no longer usable.
        raise HTTPException(410, f"invite is {state}")
    return inv


# --------------------------------------------------------------------- #
# Owner: invite management
# --------------------------------------------------------------------- #
class InviteCreateIn(BaseModel):
    label: str = Field("", max_length=120)
    partner_name_hint: Optional[str] = Field(None, max_length=120)
    partner_email_hint: Optional[EmailStr] = None
    expires_at: Optional[datetime] = None
    # Bind this invite to one of the owner's TradingAccounts.
    trading_account_id: Optional[int] = None
    # Instant-run on submit. Only honored for a DEMO bound account.
    auto_start: bool = False


class InviteOut(BaseModel):
    id: int
    token: str
    url_path: str
    label: str
    partner_name_hint: Optional[str]
    partner_email_hint: Optional[str]
    trading_account_id: Optional[int]
    account_label: Optional[str]
    account_env: Optional[str]
    auto_start: bool
    state: str
    created_at: Optional[str]
    expires_at: Optional[str]
    used_at: Optional[str]
    submission_id: Optional[int]


def _invite_out(inv: PartnerInvite, db: Session) -> InviteOut:
    sub = inv.submissions[0] if inv.submissions else None
    acct = (
        db.get(TradingAccount, inv.trading_account_id)
        if inv.trading_account_id
        else None
    )
    return InviteOut(
        id=inv.id,
        token=inv.token,
        url_path=f"/invite/{inv.token}",
        label=inv.label or "",
        partner_name_hint=inv.partner_name_hint,
        partner_email_hint=inv.partner_email_hint,
        trading_account_id=inv.trading_account_id,
        account_label=acct.label if acct else None,
        account_env=acct.tradelocker_env if acct else None,
        auto_start=bool(inv.auto_start),
        state=_invite_state(inv),
        created_at=inv.created_at.isoformat() if inv.created_at else None,
        expires_at=inv.expires_at.isoformat() if inv.expires_at else None,
        used_at=inv.used_at.isoformat() if inv.used_at else None,
        submission_id=sub.id if sub else None,
    )


@router.post("/partner-invites", response_model=InviteOut)
def create_invite(
    payload: InviteCreateIn,
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> InviteOut:
    auto_start = bool(payload.auto_start)
    if payload.trading_account_id is not None:
        # Must be an account THIS owner controls.
        account = _owner_or_403(db, payload.trading_account_id, user)
        # Instant-run is demo-only. A live account always goes through manual
        # approval — the owner flips it to live themselves.
        if auto_start and (account.tradelocker_env or "demo") != "demo":
            raise HTTPException(
                400,
                "auto_start is only allowed on a demo account; "
                "live strategies require manual approval",
            )
    elif auto_start:
        raise HTTPException(
            400, "auto_start requires a bound trading_account_id (demo account)"
        )

    inv = PartnerInvite(
        label=payload.label or "",
        partner_name_hint=payload.partner_name_hint,
        partner_email_hint=(
            str(payload.partner_email_hint).lower()
            if payload.partner_email_hint
            else None
        ),
        created_by_user_id=user.id,
        trading_account_id=payload.trading_account_id,
        auto_start=auto_start,
        expires_at=payload.expires_at,
    )
    db.add(inv)
    db.flush()
    record_audit(
        db, user=user, action="partner_invite_created",
        details={
            "invite_id": inv.id,
            "label": inv.label,
            "trading_account_id": payload.trading_account_id,
            "auto_start": auto_start,
        },
        client_ip=_client_ip(request),
    )
    db.commit()
    db.refresh(inv)
    return _invite_out(inv, db)


@router.get("/partner-invites", response_model=list[InviteOut])
def list_invites(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[InviteOut]:
    rows = (
        db.query(PartnerInvite)
        .filter(PartnerInvite.created_by_user_id == user.id)
        .order_by(PartnerInvite.id.desc())
        .all()
    )
    return [_invite_out(i, db) for i in rows]


@router.delete("/partner-invites/{invite_id}")
def revoke_invite(
    invite_id: int,
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    inv = db.get(PartnerInvite, invite_id)
    if inv is None or inv.created_by_user_id != user.id:
        raise HTTPException(404, "invite not found")
    if inv.revoked_at is not None:
        return {"status": "already_revoked"}
    inv.revoked_at = datetime.utcnow()
    record_audit(
        db, user=user, action="partner_invite_revoked",
        details={"invite_id": inv.id}, client_ip=_client_ip(request),
    )
    db.commit()
    return {"status": "revoked"}


# --------------------------------------------------------------------- #
# Public: invite landing + submit
# --------------------------------------------------------------------- #
@router.get("/invite/{token}")
def get_invite(token: str, db: Session = Depends(get_db)) -> dict:
    """Public — what the upload page needs to render. No owner data leaks."""
    inv = _resolve_active_invite(db, token)
    acct = (
        db.get(TradingAccount, inv.trading_account_id)
        if inv.trading_account_id
        else None
    )
    # Instant-run is offered only when the invite is auto_start AND bound to a
    # demo account. The public page shows the (non-sensitive) account label so
    # the partner knows which account their strategy will run on.
    instant = bool(
        inv.auto_start and acct and (acct.tradelocker_env or "demo") == "demo"
    )
    return {
        "token": inv.token,
        "label": inv.label or "",
        "partner_name_hint": inv.partner_name_hint,
        "partner_email_hint": inv.partner_email_hint,
        "expires_at": inv.expires_at.isoformat() if inv.expires_at else None,
        "account_label": acct.label if acct else None,
        "account_env": acct.tradelocker_env if acct else None,
        "instant_start": instant,
    }


@router.post("/invite/{token}/submit")
async def submit_strategy(
    token: str,
    request: Request,
    partner_name: str = Form(...),
    partner_email: EmailStr = Form(...),
    strategy_name: str = Form(...),
    delivery_type: str = Form(...),  # "source" | "http"
    instruments_csv: str = Form("NAS100"),
    timeframe: str = Form("1m"),
    params_json: Optional[str] = Form(None),
    backtest_notes: Optional[str] = Form(None),
    endpoint_url: Optional[str] = Form(None),
    endpoint_secret: Optional[str] = Form(None),
    file: Optional[UploadFile] = File(None),
    db: Session = Depends(get_db),
) -> dict:
    inv = _resolve_active_invite(db, token)

    delivery_type = (delivery_type or "").strip().lower()
    if delivery_type not in ("source", "http"):
        raise HTTPException(400, "delivery_type must be 'source' or 'http'")

    # Validate params_json is well-formed if provided.
    if params_json:
        try:
            json.loads(params_json)
        except (ValueError, TypeError):
            raise HTTPException(400, "params_json must be valid JSON")

    base_slug = _slugify(strategy_name)
    source_code: Optional[str] = None
    source_filename: Optional[str] = None
    ast_scan_json: Optional[str] = None
    enc_secret: Optional[str] = None
    stored_endpoint: Optional[str] = None

    if delivery_type == "source":
        if file is None:
            raise HTTPException(400, "a .py file is required for source delivery")
        raw = await file.read()
        if len(raw) > _MAX_UPLOAD_BYTES:
            raise HTTPException(413, "uploaded file is too large")
        try:
            source_code = raw.decode("utf-8")
        except UnicodeDecodeError:
            raise HTTPException(400, "file must be UTF-8 encoded Python source")
        source_filename = file.filename or "strategy.py"

        result = validate_strategy_source(source_code)
        ast_scan_json = json.dumps(result.to_dict())
        if not result.ok:
            # Reject WITHOUT consuming the invite so the partner can fix and
            # resubmit on the same link.
            raise HTTPException(
                422,
                {
                    "error": "strategy_failed_validation",
                    "message": "The uploaded strategy did not pass the safety scan.",
                    "findings": result.to_dict()["findings"],
                },
            )
        # Prefer the class's own declared name as the slug so the registry
        # key matches what the partner wrote.
        if result.declared_name:
            base_slug = _slugify(result.declared_name)
    else:  # http
        if not endpoint_url or not endpoint_url.lower().startswith("https://"):
            raise HTTPException(400, "endpoint_url must be an https:// URL")
        if not endpoint_secret:
            raise HTTPException(400, "endpoint_secret is required for http delivery")
        stored_endpoint = endpoint_url.strip()
        enc_secret = encrypt(endpoint_secret.strip())

    slug = _unique_slug(db, base_slug)

    sub = PartnerSubmission(
        invite_id=inv.id,
        partner_name=partner_name.strip(),
        partner_email=str(partner_email).lower(),
        strategy_name=strategy_name.strip(),
        strategy_slug=slug,
        instruments_csv=instruments_csv.strip() or "NAS100",
        timeframe=timeframe.strip() or "1m",
        params_json=params_json,
        backtest_notes=backtest_notes,
        delivery_type=delivery_type,
        source_code=source_code,
        source_filename=source_filename,
        endpoint_url=stored_endpoint,
        endpoint_secret=enc_secret,
        ast_scan_json=ast_scan_json,
        status="pending",
    )
    db.add(sub)
    db.flush()  # sub.id

    # Instant demo path: if this invite is bound to a DEMO account with
    # auto_start, provision + start now with no approval gate. Live accounts
    # (or invites with no bound account) always fall through to manual review.
    account = (
        db.get(TradingAccount, inv.trading_account_id)
        if inv.trading_account_id
        else None
    )
    owner = db.get(User, inv.created_by_user_id)
    do_auto = bool(
        inv.auto_start
        and account is not None
        and owner is not None
        and (account.tradelocker_env or "demo") == "demo"
    )

    if do_auto:
        # _provision_partner_strategy raises (→ rollback, invite intact) if the
        # strategy can't load — so a bad upload never burns the link.
        bot, grant, partner = _provision_partner_strategy(
            db,
            sub=sub,
            account=account,
            owner=owner,
            expires_at=None,
            instruments=sub.instruments_csv,
        )
        inv.used_at = datetime.utcnow()  # consume only after a clean provision
        record_audit(
            db, user=owner, action="partner_submission_auto_approved",
            details={
                "submission_id": sub.id,
                "invite_id": inv.id,
                "partner_email": sub.partner_email,
                "bot_id": bot.id,
                "grant_id": grant.id,
                "strategy_slug": sub.strategy_slug,
                "demo_account_id": account.id,
            },
            account_id=account.id, client_ip=_client_ip(request),
        )
        db.commit()  # persist provision + invite consumption BEFORE starting

        started, sa_id, start_err = await _try_autostart_demo(
            db, bot=bot, account=account, owner=owner, timeframe=sub.timeframe
        )
        return {
            "status": "running" if started else "approved",
            "submission_id": sub.id,
            "strategy_slug": sub.strategy_slug,
            "bot_id": bot.id,
            "started": started,
            "strategy_account_id": sa_id,
            "message": (
                "Your strategy is live on the demo account now — watch it take "
                "trades in the dashboard."
                if started
                else (
                    "Your strategy was accepted and registered, but the demo "
                    f"runner did not auto-start ({start_err}). The owner will "
                    "start it manually."
                )
            ),
        }

    # Default: hold for manual review. Single-use — consume now.
    inv.used_at = datetime.utcnow()
    record_audit(
        db, user=None, action="partner_submission_received",
        details={
            "submission_id": sub.id,
            "invite_id": inv.id,
            "partner_email": sub.partner_email,
            "delivery_type": delivery_type,
            "slug": slug,
        },
        client_ip=_client_ip(request),
    )
    db.commit()
    return {
        "status": "received",
        "submission_id": sub.id,
        "strategy_slug": slug,
        "message": (
            "Thanks — your strategy was received and is pending review. "
            "You'll be granted access once it's approved."
        ),
    }


# --------------------------------------------------------------------- #
# Owner: review + approve/reject
# --------------------------------------------------------------------- #
def _submission_out(sub: PartnerSubmission, *, include_source: bool = False) -> dict:
    out = {
        "id": sub.id,
        "invite_id": sub.invite_id,
        "partner_name": sub.partner_name,
        "partner_email": sub.partner_email,
        "strategy_name": sub.strategy_name,
        "strategy_slug": sub.strategy_slug,
        "instruments_csv": sub.instruments_csv,
        "timeframe": sub.timeframe,
        "params_json": sub.params_json,
        "backtest_notes": sub.backtest_notes,
        "delivery_type": sub.delivery_type,
        "endpoint_url": sub.endpoint_url,
        "source_filename": sub.source_filename,
        "has_source": bool(sub.source_code),
        "ast_scan": json.loads(sub.ast_scan_json) if sub.ast_scan_json else None,
        "status": sub.status,
        "rejection_reason": sub.rejection_reason,
        "approved_bot_id": sub.approved_bot_id,
        "created_at": sub.created_at.isoformat() if sub.created_at else None,
        "reviewed_at": sub.reviewed_at.isoformat() if sub.reviewed_at else None,
    }
    if include_source:
        out["source_code"] = sub.source_code
    return out


@router.get("/partner-submissions")
def list_submissions(
    status: Optional[str] = None,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    q = db.query(PartnerSubmission)
    if status:
        q = q.filter(PartnerSubmission.status == status)
    rows = q.order_by(PartnerSubmission.id.desc()).all()
    return {"items": [_submission_out(s) for s in rows]}


@router.get("/partner-submissions/{submission_id}")
def get_submission(
    submission_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    sub = db.get(PartnerSubmission, submission_id)
    if sub is None:
        raise HTTPException(404, "submission not found")
    return _submission_out(sub, include_source=True)


def _provision_partner_strategy(
    db: Session,
    *,
    sub: PartnerSubmission,
    account: TradingAccount,
    owner: User,
    expires_at: Optional[datetime],
    instruments: str,
) -> tuple[Bot, AccountAccessGrant, User]:
    """Register the strategy + create the viewer grant, partner bot, and state.

    Shared by manual approval and the instant demo path. Raises HTTPException
    on a load failure (so nothing half-provisions). Does NOT commit — the
    caller owns the transaction.
    """
    partner = get_or_create_user(db, sub.partner_email)
    if partner.id == owner.id:
        raise HTTPException(400, "cannot grant the owner access to their own account")

    # Register the strategy into the runtime registry FIRST — if the source
    # can't load, abort before issuing a grant or creating a bot.
    try:
        partner_loader.load_submission(sub)
    except Exception as exc:  # noqa: BLE001
        logger.warning("provision: strategy load failed for %s: %s", sub.strategy_slug, exc)
        raise HTTPException(400, f"strategy failed to load: {exc}")

    grant = AccountAccessGrant(
        account_id=account.id,
        grantee_user_id=partner.id,
        role=AccountAccessRole.viewer,
        granted_by_user_id=owner.id,
        expires_at=expires_at,
        allowed_instruments_csv=instruments,
    )
    db.add(grant)

    bot_slug = _unique_slug(db, sub.strategy_slug)
    bot = Bot(
        name=sub.strategy_name,
        slug=bot_slug,
        description=f"Partner strategy from {sub.partner_name} ({sub.partner_email})",
        strategy_type=StrategyType.partner,
        strategy_slug=sub.strategy_slug,
        instruments_csv=sub.instruments_csv,
    )
    db.add(bot)
    db.flush()  # need bot.id

    db.add(
        StrategyState(
            bot_id=bot.id,
            timeframe=sub.timeframe,
            is_running=False,
            confidence_threshold=0.8,
            max_concurrent=1,
            config_json=sub.params_json,
        )
    )

    sub.status = "approved"
    sub.reviewed_by_user_id = owner.id
    sub.reviewed_at = datetime.utcnow()
    sub.approved_bot_id = bot.id
    return bot, grant, partner


async def _try_autostart_demo(
    db: Session, *, bot: Bot, account: TradingAccount, owner: User, timeframe: str
) -> tuple[bool, Optional[int], Optional[str]]:
    """Bind the partner bot to the demo account + start an isolated runner.

    Best-effort: returns (started, strategy_account_id, error). A failure here
    (account already bound, owner has no broker creds, etc.) never rolls back
    the approval — the strategy is still provisioned; the owner can start it
    manually. Hard-gated to demo by the caller.
    """
    try:
        sa = (
            db.query(StrategyAccount)
            .filter(StrategyAccount.bot_id == bot.id)
            .first()
        )
        if sa is None:
            sa = StrategyAccount(
                bot_id=bot.id,
                user_id=owner.id,
                label=f"{bot.name} (demo)",
                tradelocker_account_id=account.tradelocker_account_id,
                tradelocker_acc_num=account.tradelocker_acc_num or "1",
                tradelocker_env=account.tradelocker_env or "demo",
                timeframe=timeframe,
            )
            db.add(sa)
            db.flush()
        sa_id = sa.id
        # Commit so the runner's own DB session (SessionLocal) sees the binding.
        db.commit()
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        logger.warning("autostart: binding failed for bot %s: %s", bot.id, exc)
        return False, None, f"binding failed: {exc}"

    try:
        from app.strategies.isolation import IsolatedRunner

        await IsolatedRunner.start(SessionLocal, sa_id)
        return True, sa_id, None
    except Exception as exc:  # noqa: BLE001
        logger.warning("autostart: runner start failed for bot %s: %s", bot.id, exc)
        return False, sa_id, f"runner start failed: {exc}"


class ApproveIn(BaseModel):
    # The TradingAccount the partner gets scoped viewer access on.
    account_id: int
    expires_at: Optional[datetime] = None
    # Override the grant's instrument scope; defaults to the submission's.
    allowed_instruments_csv: Optional[str] = Field(None, max_length=512)


@router.post("/partner-submissions/{submission_id}/approve")
def approve_submission(
    submission_id: int,
    payload: ApproveIn,
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    sub = db.get(PartnerSubmission, submission_id)
    if sub is None:
        raise HTTPException(404, "submission not found")
    if sub.status != "pending":
        raise HTTPException(409, f"submission is already {sub.status}")

    # The grant lands on an account THIS owner controls.
    account = _owner_or_403(db, payload.account_id, user)

    instruments = payload.allowed_instruments_csv or sub.instruments_csv
    bot, grant, partner = _provision_partner_strategy(
        db,
        sub=sub,
        account=account,
        owner=user,
        expires_at=payload.expires_at,
        instruments=instruments,
    )
    bot_slug = bot.slug

    record_audit(
        db, user=user, action="partner_submission_approved",
        details={
            "submission_id": sub.id,
            "partner_email": sub.partner_email,
            "grant_id": grant.id,
            "bot_id": bot.id,
            "strategy_slug": sub.strategy_slug,
            "expires_at": payload.expires_at.isoformat() if payload.expires_at else None,
        },
        account_id=account.id, client_ip=_client_ip(request),
    )
    db.commit()
    return {
        "status": "approved",
        "submission_id": sub.id,
        "bot_id": bot.id,
        "bot_slug": bot_slug,
        "grant_id": grant.id,
        "partner_user_id": partner.id,
        "next_step": (
            "Bind this bot to a demo sub-account via POST /api/isolation/accounts, "
            "then start it with POST /api/isolation/start."
        ),
    }


class RejectIn(BaseModel):
    reason: str = Field("", max_length=1000)


@router.post("/partner-submissions/{submission_id}/reject")
def reject_submission(
    submission_id: int,
    payload: RejectIn,
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    sub = db.get(PartnerSubmission, submission_id)
    if sub is None:
        raise HTTPException(404, "submission not found")
    if sub.status != "pending":
        raise HTTPException(409, f"submission is already {sub.status}")
    sub.status = "rejected"
    sub.rejection_reason = payload.reason or ""
    sub.reviewed_by_user_id = user.id
    sub.reviewed_at = datetime.utcnow()
    record_audit(
        db, user=user, action="partner_submission_rejected",
        details={"submission_id": sub.id, "reason": payload.reason},
        client_ip=_client_ip(request),
    )
    db.commit()
    return {"status": "rejected", "submission_id": sub.id}


class InstrumentsIn(BaseModel):
    # Comma-separated symbols, e.g. "NAS100,US30,XAUUSD".
    instruments_csv: str = Field(..., min_length=1, max_length=512)
    # Restart the isolated runner so it picks up the new symbol set now.
    restart: bool = True


@router.post("/partner-submissions/{submission_id}/instruments")
async def set_instruments(
    submission_id: int,
    payload: InstrumentsIn,
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    """Update which instruments an approved partner strategy scans + trades.

    The strategy is instrument-agnostic — the runner ticks on_bar() for every
    symbol in the bot's list. Updating the list (and restarting the isolated
    runner) makes it scan the new set. Owner-only.
    """
    sub = db.get(PartnerSubmission, submission_id)
    if sub is None:
        raise HTTPException(404, "submission not found")
    if sub.status != "approved" or not sub.approved_bot_id:
        raise HTTPException(409, "submission is not approved / has no bot")
    bot = db.get(Bot, sub.approved_bot_id)
    if bot is None:
        raise HTTPException(404, "bot not found")

    # Normalize: upper-case, strip, de-dupe, preserve order.
    symbols: list[str] = []
    for raw in payload.instruments_csv.split(","):
        s = raw.strip().upper()
        if s and s not in symbols:
            symbols.append(s)
    if not symbols:
        raise HTTPException(400, "no valid instruments provided")

    bot.instruments_csv = ",".join(symbols)
    sub.instruments_csv = bot.instruments_csv
    record_audit(
        db, user=user, action="partner_instruments_updated",
        details={"submission_id": sub.id, "bot_id": bot.id, "instruments": bot.instruments_csv},
        client_ip=_client_ip(request),
    )
    db.commit()

    restarted = False
    running = False
    live_symbols = symbols
    if payload.restart:
        from app.strategies.isolation import IsolatedRunner, get_iso_runner

        existing = get_iso_runner(bot.id)
        if existing is not None:
            await existing.stop()
        sa = (
            db.query(StrategyAccount)
            .filter(
                StrategyAccount.bot_id == bot.id,
                StrategyAccount.is_active.is_(True),
            )
            .first()
        )
        if sa is not None:
            try:
                runner = await IsolatedRunner.start(
                    SessionLocal, sa.id,
                    latpfn_endpoint=os.getenv("LATPFN_ENDPOINT_URL") or None,
                )
                restarted = True
                live_symbols = runner.symbols
                running = runner.task is not None and not runner.task.done()
            except Exception as exc:  # noqa: BLE001
                logger.warning("set_instruments: runner restart failed: %s", exc)

    return {
        "status": "updated",
        "bot_id": bot.id,
        "instruments": bot.instruments_csv,
        "restarted": restarted,
        "running": running,
        "symbols": live_symbols,
    }
