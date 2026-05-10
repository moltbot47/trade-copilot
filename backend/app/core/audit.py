"""Audit-log helper.

One-line API for recording security-sensitive actions:

    from app.core.audit import record_audit
    record_audit(db, user=user, action="mfa_enabled", details={"method": "totp"})

Designed to be idempotent and best-effort — a logging failure must never
break the user-facing request. The caller passes its own DB session so
the audit row commits in the same transaction as the action it records
(or rolls back together).
"""
from __future__ import annotations

import json
import logging
from typing import Any, Optional

from sqlalchemy.orm import Session

from app.db.models import AuditLog, User

logger = logging.getLogger(__name__)


# Actions we expect to see. Defined as a constant so callers don't drift
# into typo-land. New actions are fine — this is informational, not
# enforced at the schema level.
AUDIT_ACTIONS = frozenset({
    "login_success",
    "login_failed",
    "logout",
    "account_locked",
    "account_unlocked",
    "mfa_setup_initiated",
    "mfa_enabled",
    "mfa_disabled",
    "mfa_invalid_code",
    "tradelocker_connected",
    "tradelocker_disconnected",
    "tradelocker_env_changed",
    "risk_setting_changed",
    "bot_paused",
    "bot_unpaused",
    "strategy_started",
    "strategy_stopped",
    "order_placed",
    "order_rejected",
    "manual_position_close",
    "webhook_signature_invalid",
})


def record_audit(
    db: Session,
    *,
    user: Optional[User] = None,
    action: str,
    details: Optional[dict[str, Any]] = None,
    client_ip: Optional[str] = None,
) -> None:
    """Insert an AuditLog row. Best-effort; never raises."""
    try:
        row = AuditLog(
            user_id=user.id if user else None,
            actor_email=user.email if user else None,
            action=action,
            details=json.dumps(details or {}, default=str),
            client_ip=client_ip,
        )
        db.add(row)
        # The caller's commit covers us. If they only flush, our row goes
        # along on the next commit. If they roll back, we roll back too.
    except Exception as exc:
        logger.warning("audit record failed action=%s: %s", action, exc)
