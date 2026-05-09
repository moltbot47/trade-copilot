"""User registration and identity helpers.

`get_current_user` is the single source of identity for protected routes.
It reads a signed JWT from either:
  - the `tc_session` cookie (browser sessions), or
  - an `Authorization: Bearer <token>` header (API clients).

Anything else → 401. The previous X-User-Email "trust the header" path
is removed entirely.
"""
from __future__ import annotations

import secrets

import bcrypt
from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from app.config import get_settings
from app.core.jwt import JWTError, verify_session_token
from app.db.database import get_db
from app.db.models import User
from app.schemas import UserCreate, UserOut

router = APIRouter(prefix="/users", tags=["users"])


def _hash_password(plain: str) -> str:
    return bcrypt.hashpw(plain.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def _extract_token(request: Request) -> str | None:
    """Pull session token from cookie first, then Authorization header."""
    settings = get_settings()
    cookie_val = request.cookies.get(settings.SESSION_COOKIE_NAME)
    if cookie_val:
        return cookie_val
    auth = request.headers.get("authorization")
    if auth and auth.lower().startswith("bearer "):
        return auth.split(" ", 1)[1].strip() or None
    return None


def get_current_user(
    request: Request,
    db: Session = Depends(get_db),
) -> User:
    """Return the User row for the authenticated session, or raise 401.

    Auto-creates a User row on first login (handled by /auth/login) — by
    the time a request reaches this dependency, the user must already
    exist; we only validate the token here.
    """
    token = _extract_token(request)
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="not authenticated",
            headers={"WWW-Authenticate": "Bearer"},
        )
    try:
        claims = verify_session_token(token)
    except JWTError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"invalid session: {exc}",
            headers={"WWW-Authenticate": "Bearer"},
        )
    email = claims.get("sub")
    if not email or not isinstance(email, str):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="malformed token")
    user = db.query(User).filter(User.email == email).first()
    if user is None:
        # Token valid but user vanished — treat as unauthenticated.
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="user not found")
    return user


def get_or_create_user(db: Session, email: str) -> User:
    """Idempotent fetch-or-create. Used by /auth/login.

    Preserves the MVP's frictionless "just-give-an-email" feel — but the
    session that comes out is now a signed JWT, not a spoofable header.
    """
    user = db.query(User).filter(User.email == email).first()
    if user is None:
        user = User(
            email=email,
            hashed_password=_hash_password(secrets.token_urlsafe(24)),
        )
        db.add(user)
        db.commit()
        db.refresh(user)
    return user


@router.post("", response_model=UserOut, status_code=status.HTTP_201_CREATED)
def create_user(payload: UserCreate, db: Session = Depends(get_db)) -> UserOut:
    existing = db.query(User).filter(User.email == payload.email).first()
    if existing:
        raise HTTPException(status_code=409, detail="email already registered")
    user = User(email=payload.email, hashed_password=_hash_password(payload.password))
    db.add(user)
    db.commit()
    db.refresh(user)
    return UserOut.model_validate(user)


@router.get("/me", response_model=UserOut)
def read_me(user: User = Depends(get_current_user)) -> UserOut:
    return UserOut.model_validate(user)
