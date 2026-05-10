"""MFA setup, verification, and disable endpoints.

Flow:
  1. POST /api/auth/mfa/setup  →  returns {secret, otpauth_uri}
                                  (server has now stored encrypted secret
                                   BUT mfa_enabled is still False)
  2. POST /api/auth/mfa/verify {code: "123456"}
                                  →  flips mfa_enabled=True
  3. From now on, /api/auth/login REQUIRES `mfa_code` if mfa_enabled.
  4. POST /api/auth/mfa/disable {code: "123456"}
                                  →  clears mfa_secret + mfa_enabled=False
                                     (requires a valid current code to prevent
                                      accidental lockout reversal by attacker)
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.api.users import get_current_user
from app.auth.mfa import (
    decrypt_secret,
    encrypt_secret,
    generate_secret,
    provisioning_uri,
    verify_code,
)
from app.db.database import get_db
from app.db.models import User

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/auth/mfa", tags=["auth"])


class MfaSetupResponse(BaseModel):
    secret: str             # base32 — shown ONCE to the user
    otpauth_uri: str        # for QR rendering
    enabled: bool


class MfaCodeRequest(BaseModel):
    code: str


class MfaStatusResponse(BaseModel):
    enabled: bool


@router.get("/status", response_model=MfaStatusResponse)
def mfa_status(user: User = Depends(get_current_user)) -> MfaStatusResponse:
    return MfaStatusResponse(enabled=bool(user.mfa_enabled))


@router.post("/setup", response_model=MfaSetupResponse)
def mfa_setup(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> MfaSetupResponse:
    """Generate a fresh TOTP secret for the user.

    If MFA is already enabled, refuse — user must disable first (with code)
    before generating a new secret. Prevents an attacker who hijacks a
    session from rotating the secret out from under the real user.
    """
    if user.mfa_enabled:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="mfa_already_enabled — disable first",
        )

    secret = generate_secret()
    user.mfa_secret = encrypt_secret(secret)
    user.mfa_enabled = False  # not active until /verify confirms
    db.commit()
    logger.info("mfa setup initiated for user=%s", user.email)

    return MfaSetupResponse(
        secret=secret,
        otpauth_uri=provisioning_uri(secret, user.email),
        enabled=False,
    )


@router.post("/verify", response_model=MfaStatusResponse)
def mfa_verify(
    payload: MfaCodeRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> MfaStatusResponse:
    """Verify the TOTP code from authenticator and activate MFA."""
    secret = decrypt_secret(user.mfa_secret)
    if not secret:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="mfa_not_setup — call /setup first",
        )
    if not verify_code(secret, payload.code):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="mfa_invalid_code",
        )
    user.mfa_enabled = True
    db.commit()
    logger.info("mfa enabled for user=%s", user.email)
    return MfaStatusResponse(enabled=True)


@router.post("/disable", response_model=MfaStatusResponse)
def mfa_disable(
    payload: MfaCodeRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> MfaStatusResponse:
    """Disable MFA — requires a valid current code to prevent hijack."""
    if not user.mfa_enabled:
        return MfaStatusResponse(enabled=False)
    secret = decrypt_secret(user.mfa_secret)
    if not secret or not verify_code(secret, payload.code):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="mfa_invalid_code",
        )
    user.mfa_secret = None
    user.mfa_enabled = False
    db.commit()
    logger.info("mfa disabled for user=%s", user.email)
    return MfaStatusResponse(enabled=False)
