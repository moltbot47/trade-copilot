"""TOTP-based multi-factor authentication.

Adds a second factor on top of the existing email-only login. The shared
secret is generated server-side, returned once to the user as a base32
string + otpauth URL (for QR), then stored encrypted in the DB. From
that point on, login requires a valid 6-digit code from any RFC 6238
TOTP authenticator (Google Authenticator, Authy, 1Password, etc).

The secret is encrypted with the same Fernet key as the TradeLocker
token, so key rotation handles both consistently.

Threat model:
  - Email compromise alone is no longer sufficient to take over an account.
  - Server-side secret theft (DB dump) is mitigated by Fernet encryption
    at rest — attacker needs both DB AND ENCRYPTION_KEY env var.
  - Backup codes are NOT implemented in v1 — users who lose their device
    must contact support, which here means us regenerating mfa_secret
    after manual identity verification. Add backup codes if/when we grow.
"""
from __future__ import annotations

import secrets
from typing import Optional

import pyotp

from app.core.crypto import decrypt, encrypt


# RFC 6238 standard window: ±1 step (30s) tolerance, total 90s acceptance
TOTP_WINDOW = 1
TOTP_ISSUER = "Trade Copilot"


def generate_secret() -> str:
    """Generate a fresh base32 TOTP secret (160 bits of entropy)."""
    return pyotp.random_base32()


def provisioning_uri(secret: str, email: str) -> str:
    """Return the otpauth:// URI that authenticator apps consume (for QR)."""
    totp = pyotp.TOTP(secret)
    return totp.provisioning_uri(name=email, issuer_name=TOTP_ISSUER)


def verify_code(secret: str, code: str) -> bool:
    """Validate a 6-digit TOTP code against the secret with a 1-step window."""
    if not secret or not code:
        return False
    # Strip whitespace and reject non-numeric / wrong-length input early
    code = code.strip().replace(" ", "")
    if not code.isdigit() or len(code) != 6:
        return False
    try:
        totp = pyotp.TOTP(secret)
        return totp.verify(code, valid_window=TOTP_WINDOW)
    except Exception:
        return False


def encrypt_secret(plain_secret: str) -> str:
    """Encrypt the base32 secret for DB storage."""
    return encrypt(plain_secret)


def decrypt_secret(stored: Optional[str]) -> Optional[str]:
    """Reverse of encrypt_secret; returns None on missing/invalid input."""
    if not stored:
        return None
    try:
        return decrypt(stored)
    except Exception:
        return None


def generate_recovery_code() -> str:
    """16-character recovery code for one-time emergency access (currently unused;
    reserved for backup-codes feature).
    """
    return secrets.token_urlsafe(12)
