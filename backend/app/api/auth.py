"""Authentication endpoints: login, logout, and session inspection.

Login issues a signed JWT in an HttpOnly+SameSite=Lax cookie named
`tc_session`. The MVP "no password" feel is preserved (any email
auto-creates a row), but the session is now signed and non-spoofable.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, EmailStr
from sqlalchemy.orm import Session

from app.api.users import get_current_user, get_or_create_user
from app.config import get_settings
from app.core.jwt import issue_session_token
from app.core.rate_limit import limiter
from app.db.database import get_db
from app.db.models import User

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/auth", tags=["auth"])


class LoginRequest(BaseModel):
    email: EmailStr
    mfa_code: str | None = None


class LoginResponse(BaseModel):
    email: EmailStr
    exp: int  # unix seconds


class MfaRequiredResponse(BaseModel):
    """Returned with HTTP 401 when an mfa_enabled user logs in without a code."""
    detail: str = "mfa_required"
    email: EmailStr


class MeResponse(BaseModel):
    email: EmailStr
    tradelocker_account_id: str | None = None
    tradelocker_env: str = "demo"


def _set_session_cookie(response: Response, token: str, max_age_seconds: int) -> None:
    settings = get_settings()
    response.set_cookie(
        key=settings.SESSION_COOKIE_NAME,
        value=token,
        max_age=max_age_seconds,
        httponly=True,
        samesite="lax",
        secure=settings.cookie_secure,
        path="/",
    )


def _clear_session_cookie(response: Response) -> None:
    settings = get_settings()
    response.delete_cookie(
        key=settings.SESSION_COOKIE_NAME,
        path="/",
        samesite="lax",
        secure=settings.cookie_secure,
    )


@router.post("/login", response_model=LoginResponse)
@limiter.limit("10/minute")
def login(
    request: Request,
    payload: LoginRequest,
    response: Response,
    db: Session = Depends(get_db),
) -> LoginResponse:
    """Issue a session cookie for the given email.

    First-time emails auto-create a User row (MVP: no password barrier).
    Rate-limited to 10/min per IP to deter enumeration.
    """
    from fastapi import HTTPException, status as http_status

    settings = get_settings()
    # Detect signup vs. returning login BEFORE create — used for the admin
    # Discord alert further down. Cheap pre-query; the row state is unchanged.
    is_new_signup = (
        db.query(User).filter(User.email == str(payload.email)).first() is None
    )
    user = get_or_create_user(db, str(payload.email))
    from app.core.audit import record_audit
    from datetime import datetime, timedelta
    client_ip = request.client.host if request.client else None

    # Lockout check (OWASP ASVS V2): if the account is currently locked,
    # refuse even before checking MFA. Lockout window expires automatically
    # — no operator intervention needed.
    LOCKOUT_THRESHOLD = 5
    LOCKOUT_MINUTES = 15
    if user.locked_until and user.locked_until > datetime.utcnow():
        record_audit(db, user=user, action="login_failed",
                     details={"reason": "account_locked"}, client_ip=client_ip)
        db.commit()
        raise HTTPException(
            status_code=http_status.HTTP_429_TOO_MANY_REQUESTS,
            detail="account_locked",
            headers={"Retry-After": str(int((user.locked_until - datetime.utcnow()).total_seconds()))},
        )

    # MFA gate — if this user has enabled TOTP, the email is not enough.
    # We reject with a 401 + a distinguishable detail string so the frontend
    # can prompt for the code without leaking whether the email exists.
    if user.mfa_enabled:
        from app.auth.mfa import decrypt_secret, verify_code

        if not payload.mfa_code:
            record_audit(db, user=user, action="login_failed",
                         details={"reason": "mfa_required"}, client_ip=client_ip)
            db.commit()
            raise HTTPException(
                status_code=http_status.HTTP_401_UNAUTHORIZED,
                detail="mfa_required",
            )
        secret = decrypt_secret(user.mfa_secret)
        if not secret or not verify_code(secret, payload.mfa_code):
            # Failed MFA counts toward lockout — a brute-forcer cycling
            # through codes shouldn't get infinite attempts.
            user.failed_login_count = (user.failed_login_count or 0) + 1
            if user.failed_login_count >= LOCKOUT_THRESHOLD:
                user.locked_until = datetime.utcnow() + timedelta(minutes=LOCKOUT_MINUTES)
                record_audit(db, user=user, action="account_locked",
                             details={"after_failures": user.failed_login_count}, client_ip=client_ip)
            record_audit(db, user=user, action="mfa_invalid_code", client_ip=client_ip)
            db.commit()
            raise HTTPException(
                status_code=http_status.HTTP_401_UNAUTHORIZED,
                detail="mfa_invalid_code",
            )

    token, expires_at = issue_session_token(user.email)
    max_age = settings.SESSION_TTL_DAYS * 24 * 60 * 60
    _set_session_cookie(response, token, max_age)
    logger.info("login: %s (exp=%s)", user.email, expires_at.isoformat())
    # Successful login — reset lockout counter
    if (user.failed_login_count or 0) > 0:
        record_audit(db, user=user, action="account_unlocked",
                     details={"prior_failed": user.failed_login_count}, client_ip=client_ip)
    user.failed_login_count = 0
    user.locked_until = None
    record_audit(db, user=user, action="login_success", client_ip=client_ip)
    db.commit()

    # Fire-and-forget admin Discord alert. Goes to the global operator
    # webhook (DISCORD_WEBHOOK_URL). Per-user webhooks are explicitly NOT
    # used here — that would leak the customer base to each customer.
    try:
        from app.integrations.discord_signals import post_admin_event_fire_and_forget

        post_admin_event_fire_and_forget(
            event="signup" if is_new_signup else "login",
            user_email=user.email,
            details={
                "from_ip": client_ip or "?",
                "user_id": str(user.id),
            },
        )
    except Exception as exc:  # noqa: BLE001 — never block login on Discord
        logger.debug("admin alert (login/signup) skipped: %s", exc)

    # Boot the TradeLocker relay if the user has a stored token.
    # Best-effort — failures here must not block login.
    if user.tradelocker_token and user.tradelocker_account_id:
        try:
            from app.ws.relay_manager import relay_manager

            relay_manager.start_for_user(user.id)
        except Exception as exc:  # pragma: no cover — defensive
            logger.debug("relay_manager.start_for_user skipped: %s", exc)

    return LoginResponse(email=user.email, exp=int(expires_at.timestamp()))


@router.post("/logout")
def logout(
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
) -> dict[str, str]:
    """Clear the session cookie. Always 200 — idempotent."""
    _clear_session_cookie(response)

    # Stop any running TL relay for this user. We resolve the user from
    # the cookie/header (best-effort) so we don't require auth on logout.
    try:
        from app.api.users import _extract_token

        token = _extract_token(request)
        if token:
            from app.core.jwt import JWTError, verify_session_token

            try:
                claims = verify_session_token(token)
                email = claims.get("sub")
                if email:
                    user = db.query(User).filter(User.email == email).first()
                    if user is not None:
                        from app.ws.relay_manager import relay_manager
                        import asyncio

                        try:
                            asyncio.get_running_loop().create_task(
                                relay_manager.stop_for_user(user.id)
                            )
                        except RuntimeError:
                            # No running loop (sync context) — schedule via run
                            try:
                                asyncio.run(relay_manager.stop_for_user(user.id))
                            except Exception:
                                pass
            except JWTError:
                pass
    except Exception as exc:  # pragma: no cover — defensive
        logger.debug("logout relay-stop skipped: %s", exc)

    return {"status": "logged_out"}


@router.get("/me", response_model=MeResponse)
def me(user: User = Depends(get_current_user)) -> MeResponse:
    """Return the current user's identity. 401 if no valid session."""
    return MeResponse(
        email=user.email,
        tradelocker_account_id=user.tradelocker_account_id,
        tradelocker_env=user.tradelocker_env or "demo",
    )


class AuditLogEntry(BaseModel):
    ts: str
    action: str
    details: str
    client_ip: str | None = None


@router.get("/audit-log", response_model=list[AuditLogEntry])
def my_audit_log(
    limit: int = 50,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[AuditLogEntry]:
    """Return the current user's audit log (most recent N entries).

    Owner-only — each user can see only their own actions. Useful for
    "was that me?" verification after a suspicious notification.
    """
    from app.db.models import AuditLog

    rows = (
        db.query(AuditLog)
        .filter(AuditLog.user_id == user.id)
        .order_by(AuditLog.id.desc())
        .limit(max(1, min(limit, 200)))
        .all()
    )
    return [
        AuditLogEntry(
            ts=r.ts.isoformat() if r.ts else "",
            action=r.action,
            details=r.details or "{}",
            client_ip=r.client_ip,
        )
        for r in rows
    ]
