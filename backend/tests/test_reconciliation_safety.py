"""Reconciliation MUST NEVER modify a position the bot did not initiate.

This is the regression test for the 2026-05-20 incident where the
"auto-protect" feature in reconciliation slapped 0.5%-adverse stop
losses onto users' manual day-trading positions because they did not
match a DB-tracked CohortLeg.

The invariant under test is one sentence:
  ``reconcile_user`` does not call ``client.modify_position`` for any
  position whose id is NOT in the tracked-leg set.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy.orm import sessionmaker

from app.core.crypto import encrypt
from app.db.models import (
    Cohort,
    CohortLeg,
    CohortStatus,
    StrategyType,
    User,
)
from app.strategies import reconciliation


@pytest.fixture()
def session_factory(db_engine):
    return sessionmaker(
        autocommit=False, autoflush=False, bind=db_engine, future=True
    )


def _user(db_session) -> User:
    u = User(
        email="manual@x.com",
        hashed_password="x",
        tradelocker_token=encrypt("good-token"),
        tradelocker_account_id="744159",
        tradelocker_acc_num="1",
        tradelocker_env="live",
    )
    db_session.add(u)
    db_session.commit()
    return u


# A position the broker shows but our DB has never heard of — i.e. the
# user's own manual trade. Crucially has_sl=False AND has_tp=False so the
# OLD code would have hit the auto-protect branch and modified it.
_MANUAL_POSITION = {
    "id": "72057594040885614",
    "side": "buy",
    "qty": 0.04,
    "avgPrice": 29287.73,
    "tradableInstrumentId": 8530,
    # No stopLossId, no takeProfitId → previously this is what triggered
    # the auto-protect modify_position call.
}


@pytest.mark.asyncio
async def test_reconciliation_never_modifies_untracked_position(
    db_session, session_factory
):
    """The regression for the cmercer incident: a manual position with no
    SL/TP must NOT be modified by reconciliation."""
    user = _user(db_session)

    mock_client = AsyncMock()
    mock_client.get_positions = AsyncMock(return_value=[_MANUAL_POSITION])
    mock_client.modify_position = AsyncMock()

    with patch.object(reconciliation, "SessionLocal", session_factory), patch.object(
        reconciliation, "TradeLockerClient", return_value=mock_client
    ):
        summary = await reconciliation.reconcile_user(user.id)

    # Position was correctly identified as untracked.
    assert summary["untracked_positions"] == 1
    # No auto-protect counter exists anymore — the field shouldn't be set.
    assert "auto_protected" not in summary
    # THE INVARIANT: modify_position MUST NOT have been called.
    mock_client.modify_position.assert_not_called()


@pytest.mark.asyncio
async def test_reconciliation_still_logs_drift_for_untracked(
    db_session, session_factory, caplog
):
    """Stripping the modify must not strip the warning — operators still
    need to see manual / orphan positions in the logs."""
    import logging

    user = _user(db_session)
    mock_client = AsyncMock()
    mock_client.get_positions = AsyncMock(return_value=[_MANUAL_POSITION])
    mock_client.modify_position = AsyncMock()

    # Bypass the per-key 60-min cooldown so the warning always fires.
    reconciliation._drift_alerted.clear()

    with patch.object(reconciliation, "SessionLocal", session_factory), patch.object(
        reconciliation, "TradeLockerClient", return_value=mock_client
    ), caplog.at_level(logging.WARNING, logger="app.strategies.reconciliation"):
        await reconciliation.reconcile_user(user.id)

    assert any(
        "UNTRACKED BROKER POSITION" in rec.getMessage() for rec in caplog.records
    ), "drift warning must still fire for untracked positions"
    mock_client.modify_position.assert_not_called()


@pytest.mark.asyncio
async def test_reconciliation_marks_ghost_leg_closed_no_modify(
    db_session, session_factory
):
    """When a DB-tracked leg references a broker position that's gone, we
    mark the leg closed — but we still don't issue any modify_position."""
    from app.db.models import Bot

    user = _user(db_session)
    bot = Bot(
        name="LaT",
        slug="lat-rec",
        strategy_type=StrategyType.latpfn_quant,
        instruments_csv="BTCUSD",
        webhook_secret="s",
    )
    db_session.add(bot)
    db_session.commit()
    cohort = Cohort(
        bot_id=bot.id,
        user_id=user.id,
        instrument="BTCUSD",
        side="buy",
        timeframe="1m",
        status=CohortStatus.open,
        atr_at_entry=400.0,
        initial_entry_price=80_000.0,
        initial_stop_loss=79_600.0,
        initial_take_profit=82_000.0,
        weighted_avg_entry=80_000.0,
        total_qty=0.01,
        current_stop=79_600.0,
    )
    db_session.add(cohort)
    db_session.commit()
    leg = CohortLeg(
        cohort_id=cohort.id,
        role="entry",
        side="buy",
        entry_price=80_000.0,
        qty=0.01,
        tradelocker_position_id="GHOST-POS-1",
        is_open=True,
    )
    db_session.add(leg)
    db_session.commit()
    leg_id = leg.id

    mock_client = AsyncMock()
    mock_client.get_positions = AsyncMock(return_value=[])  # broker has nothing
    mock_client.modify_position = AsyncMock()

    with patch.object(reconciliation, "SessionLocal", session_factory), patch.object(
        reconciliation, "TradeLockerClient", return_value=mock_client
    ):
        summary = await reconciliation.reconcile_user(user.id)

    assert summary["ghost_cohorts"] == 1
    mock_client.modify_position.assert_not_called()
    db_session.expire_all()
    refreshed = db_session.get(CohortLeg, leg_id)
    assert refreshed.is_open is False
