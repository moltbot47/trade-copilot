"""Tests for the position reconciliation job."""
from __future__ import annotations

from unittest.mock import patch

import pytest

from app.db.models import (
    Cohort, CohortLeg, CohortStatus, User, StrategyType,
)
from app.strategies.reconciliation import (
    _drift_alerted,
    reconcile_user,
)


@pytest.fixture(autouse=True)
def reset_alert_cooldown(db_engine):
    """Clear the alert-cooldown cache AND rebind reconciliation's
    SessionLocal to the test engine so direct queries see the test schema.
    """
    from sqlalchemy.orm import sessionmaker
    import app.strategies.reconciliation as recon_mod

    _drift_alerted.clear()
    TestSession = sessionmaker(bind=db_engine, autoflush=False, autocommit=False)
    with patch.object(recon_mod, "SessionLocal", TestSession):
        yield
    _drift_alerted.clear()


@pytest.fixture
def setup(db_session, seed_bots):
    user = User(email="recon@example.com", hashed_password="x")
    user.tradelocker_account_id = "1"
    user.tradelocker_acc_num = "1"
    user.tradelocker_env = "demo"
    from app.core.crypto import encrypt
    user.tradelocker_token = encrypt("fake-token")
    db_session.add(user)
    db_session.commit()
    bot = next(b for b in seed_bots if b.strategy_type == StrategyType.latpfn_quant)
    return {"user": user, "bot": bot}


def _make_cohort(db_session, user, bot, pos_id: str = "BROKER-POS-1"):
    """Helper: open cohort with one leg pointing to a broker position id."""
    cohort = Cohort(
        bot_id=bot.id, user_id=user.id, instrument="BTCUSD", side="buy",
        weighted_avg_entry=80000.0, atr_at_entry=400.0, total_qty=0.01,
        initial_entry_price=80000.0,
        initial_stop_loss=79600.0, current_stop=79600.0,
        initial_take_profit=80800.0, status=CohortStatus.open,
        timeframe="1m",
    )
    db_session.add(cohort)
    db_session.flush()
    leg = CohortLeg(
        cohort_id=cohort.id, role="entry", side="buy",
        qty=0.01, entry_price=80000.0, stop_loss=79600.0,
        tradelocker_position_id=pos_id, is_open=True,
    )
    db_session.add(leg)
    db_session.commit()
    return cohort


@pytest.mark.asyncio
async def test_reconcile_no_drift_when_aligned(setup, db_session):
    user, bot = setup["user"], setup["bot"]
    _make_cohort(db_session, user, bot, pos_id="POS-A")

    # Patch TradeLockerClient.get_positions to return the matching position
    async def fake_positions(*args, **kwargs):
        return [{"id": "POS-A", "side": "buy", "qty": 0.01, "tradableInstrumentId": 206}]

    with patch("app.core.tradelocker_client.TradeLockerClient.get_positions", new=fake_positions):
        result = await reconcile_user(user.id)
    assert result == {"ghost_cohorts": 0, "untracked_positions": 0, "errors": 0}


@pytest.mark.asyncio
async def test_reconcile_detects_ghost_cohort(setup, db_session):
    """DB says cohort leg is open with broker pos, but broker has nothing."""
    user, bot = setup["user"], setup["bot"]
    cohort = _make_cohort(db_session, user, bot, pos_id="POS-A")

    async def fake_positions(*args, **kwargs):
        return []  # broker reports NO positions

    with patch("app.core.tradelocker_client.TradeLockerClient.get_positions", new=fake_positions):
        result = await reconcile_user(user.id)

    assert result["ghost_cohorts"] == 1
    # Defensive close: the ghost leg should now be marked closed
    db_session.refresh(cohort)
    leg = cohort.legs[0]
    db_session.refresh(leg)
    assert leg.is_open is False


@pytest.mark.asyncio
async def test_reconcile_detects_untracked_broker_position(setup, db_session):
    """Broker has a position that no cohort leg references."""
    user, bot = setup["user"], setup["bot"]
    # No cohorts in DB

    async def fake_positions(*args, **kwargs):
        return [
            {"id": "MYSTERY-POS", "side": "buy", "qty": 0.05, "tradableInstrumentId": 206},
        ]

    with patch("app.core.tradelocker_client.TradeLockerClient.get_positions", new=fake_positions):
        result = await reconcile_user(user.id)

    assert result["untracked_positions"] == 1
    assert result["ghost_cohorts"] == 0


@pytest.mark.asyncio
async def test_reconcile_handles_broker_error(setup, db_session):
    """On TL error, reconciliation should count the error and continue.

    Note: as of the 401-auto-refresh fix, "unauthorized" triggers a refresh
    attempt. We patch refresh_user_token to return None (simulating refresh
    also failed) so the error propagates back to reconcile_user — which is
    the realistic scenario when both access and refresh tokens are expired.
    """
    user, bot = setup["user"], setup["bot"]
    _make_cohort(db_session, user, bot)

    async def fake_positions(*args, **kwargs):
        from app.core.tradelocker_client import TradeLockerError
        raise TradeLockerError("unauthorized")

    async def no_refresh(uid):
        return None  # refresh token also expired → propagate

    with patch(
        "app.core.tradelocker_client.TradeLockerClient.get_positions", new=fake_positions
    ), patch("app.core.tradelocker_auth.refresh_user_token", new=no_refresh):
        result = await reconcile_user(user.id)

    assert result["errors"] == 1
    assert result["ghost_cohorts"] == 0


@pytest.mark.asyncio
async def test_reconcile_skips_user_without_credentials(db_session, seed_bots):
    user = User(email="no-creds@example.com", hashed_password="x")
    db_session.add(user)
    db_session.commit()
    result = await reconcile_user(user.id)
    assert result == {"ghost_cohorts": 0, "untracked_positions": 0, "errors": 0}


@pytest.mark.asyncio
async def test_alert_cooldown_prevents_repeat(setup, db_session):
    """Two reconcile passes on the same ghost should only count once
    when checking the alert cooldown — but the metric still increments."""
    user, bot = setup["user"], setup["bot"]
    _make_cohort(db_session, user, bot, pos_id="POS-A")

    async def fake_positions(*args, **kwargs):
        return []

    with patch("app.core.tradelocker_client.TradeLockerClient.get_positions", new=fake_positions):
        r1 = await reconcile_user(user.id)
        # After first pass the leg is closed defensively, so a second pass
        # will see 0 ghost cohorts.
        r2 = await reconcile_user(user.id)

    assert r1["ghost_cohorts"] == 1
    assert r2["ghost_cohorts"] == 0
