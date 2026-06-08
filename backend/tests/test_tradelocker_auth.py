"""Tests for the shared TradeLocker auth helper.

Covers the three Part B/A behaviors that previously varied per caller:
  - Successful first-call (no refresh needed)
  - 401 → refresh → retry succeeds
  - 401 → refresh fails → propagate
  - validate_user_tl_session probe behavior
"""
from __future__ import annotations

from unittest.mock import patch

import pytest
from sqlalchemy.orm import sessionmaker

from app.core.crypto import encrypt
from app.core.tradelocker_client import TradeLockerError
from app.db.models import User


@pytest.fixture
def patched_session_local(db_engine):
    """Rebind tradelocker_auth.SessionLocal to the test engine."""
    import app.core.tradelocker_auth as auth_mod

    TestSession = sessionmaker(bind=db_engine, autoflush=False, autocommit=False)
    with patch.object(auth_mod, "SessionLocal", TestSession):
        yield


@pytest.fixture
def connected_user(db_session, patched_session_local):
    user = User(email="auth-test@example.com", hashed_password="x")
    user.tradelocker_account_id = "777"
    user.tradelocker_acc_num = "1"
    user.tradelocker_env = "demo"
    user.tradelocker_token = encrypt("good-access-token")
    user.tradelocker_refresh_token = encrypt("good-refresh-token")
    db_session.add(user)
    db_session.commit()
    return user


@pytest.mark.asyncio
async def test_call_with_refresh_succeeds_without_retry(connected_user):
    """If the first call succeeds, no refresh attempted, result returned."""
    from app.core.tradelocker_auth import call_with_refresh

    captured_tokens: list[str] = []

    async def op(s: dict) -> str:
        captured_tokens.append(s["token"])
        return "ok"

    result = await call_with_refresh(connected_user.id, op)

    assert result == "ok"
    assert captured_tokens == ["good-access-token"]


@pytest.mark.asyncio
async def test_call_with_refresh_retries_after_401(connected_user):
    """First call raises 401, refresh succeeds, retry uses new token."""
    from app.core.tradelocker_auth import call_with_refresh

    calls: list[str] = []

    async def op(s: dict) -> str:
        calls.append(s["token"])
        if len(calls) == 1:
            raise TradeLockerError("unauthorized (401) - token expired or invalid")
        return "retry-ok"

    async def fake_refresh(uid: int) -> str:
        # Simulate refresh_user_token persisting new tokens. Use the
        # auth module's SessionLocal (patched to the test engine).
        import app.core.tradelocker_auth as auth_mod
        db = auth_mod.SessionLocal()
        try:
            u = db.query(User).filter(User.id == uid).first()
            u.tradelocker_token = encrypt("rotated-token")
            db.commit()
        finally:
            db.close()
        return "rotated-token"

    with patch("app.core.tradelocker_auth.refresh_user_token", new=fake_refresh):
        result = await call_with_refresh(connected_user.id, op)

    assert result == "retry-ok"
    assert calls == ["good-access-token", "rotated-token"]


@pytest.mark.asyncio
async def test_call_with_refresh_propagates_when_refresh_fails(connected_user):
    """First call 401s, refresh returns None → raises original error, no retry."""
    from app.core.tradelocker_auth import call_with_refresh

    call_count = 0

    async def op(s: dict) -> str:
        nonlocal call_count
        call_count += 1
        raise TradeLockerError("unauthorized (401) - token expired or invalid")

    async def fake_refresh(uid: int) -> None:
        return None  # refresh token also expired

    with patch("app.core.tradelocker_auth.refresh_user_token", new=fake_refresh):
        with pytest.raises(TradeLockerError, match="401"):
            await call_with_refresh(connected_user.id, op)

    assert call_count == 1  # no retry attempted


@pytest.mark.asyncio
async def test_call_with_refresh_propagates_non_auth_errors(connected_user):
    """A 500 (or any non-401) error is raised immediately without refresh."""
    from app.core.tradelocker_auth import call_with_refresh

    call_count = 0

    async def op(s: dict) -> str:
        nonlocal call_count
        call_count += 1
        raise TradeLockerError("500 internal server error")

    refresh_called = False

    async def fake_refresh(uid: int) -> str:
        nonlocal refresh_called
        refresh_called = True
        return "should-not-be-called"

    with patch("app.core.tradelocker_auth.refresh_user_token", new=fake_refresh):
        with pytest.raises(TradeLockerError, match="500"):
            await call_with_refresh(connected_user.id, op)

    assert call_count == 1
    assert refresh_called is False


@pytest.mark.asyncio
async def test_call_with_refresh_raises_when_no_session(db_session, patched_session_local):
    """Users with no TL token raise ValueError, not TradeLockerError."""
    from app.core.tradelocker_auth import call_with_refresh

    user = User(email="no-tl@example.com", hashed_password="x")
    db_session.add(user)
    db_session.commit()

    async def op(s: dict) -> str:  # pragma: no cover — never called
        return "should not run"

    with pytest.raises(ValueError, match="no TradeLocker session"):
        await call_with_refresh(user.id, op)


@pytest.mark.asyncio
async def test_validate_returns_ok_when_probe_succeeds(connected_user):
    """Healthy session: probe succeeds, returns (True, 'ok')."""
    from app.core.tradelocker_auth import validate_user_tl_session

    async def fake_list_accounts(self, token):
        return [{"id": "777", "accNum": "1"}]

    with patch(
        "app.core.tradelocker_client.TradeLockerClient.list_all_accounts",
        new=fake_list_accounts,
    ):
        ok, reason = await validate_user_tl_session(connected_user.id)

    assert ok is True
    assert reason == "ok"


@pytest.mark.asyncio
async def test_validate_returns_needs_reauth_when_refresh_fails(connected_user):
    """Probe 401s, refresh returns None → (False, 'needs_reauth')."""
    from app.core.tradelocker_auth import validate_user_tl_session

    async def fake_list_accounts(self, token):
        raise TradeLockerError("unauthorized (401) - token expired or invalid")

    async def fake_refresh(uid: int) -> None:
        return None

    with patch(
        "app.core.tradelocker_client.TradeLockerClient.list_all_accounts",
        new=fake_list_accounts,
    ), patch("app.core.tradelocker_auth.refresh_user_token", new=fake_refresh):
        ok, reason = await validate_user_tl_session(connected_user.id)

    assert ok is False
    assert reason == "needs_reauth"


@pytest.mark.asyncio
async def test_validate_returns_no_session_for_disconnected_user(
    db_session, patched_session_local
):
    """User with no TL connection → (False, 'no_session')."""
    from app.core.tradelocker_auth import validate_user_tl_session

    user = User(email="disconnected@example.com", hashed_password="x")
    db_session.add(user)
    db_session.commit()

    ok, reason = await validate_user_tl_session(user.id)

    assert ok is False
    assert reason == "no_session"
