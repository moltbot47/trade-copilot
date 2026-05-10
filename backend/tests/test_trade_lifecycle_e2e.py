"""End-to-end trade lifecycle: open → scale-in → partial close → trail → exit.

Drives a TradeManager + MockBroker through a complete cohort lifecycle
and asserts state machine + DB consistency at every step. This is the
single best regression net against the ordering bugs we hit in the
2026-05-10 incident.
"""
from __future__ import annotations

import pytest

from app.db.models import (
    Bot, CohortStatus, StrategyType, TradeOutcome, User,
)
from app.strategies.trade_manager import TradeManager
from tests.fixtures.mock_broker import MockBroker


@pytest.fixture
def setup(db_session, seed_bots):
    bot = next(b for b in seed_bots if b.strategy_type == StrategyType.latpfn_quant)
    user = User(email="lifecycle@example.com", hashed_password="x")
    db_session.add(user)
    db_session.commit()
    return {
        "bot_id": bot.id,
        "user_id": user.id,
        "user": user,
        "bot": bot,
    }


@pytest.mark.asyncio
async def test_full_buy_lifecycle_exit_at_sl(db_session, setup):
    """Open BUY 0.01 @ 80000 SL 79600 → BE lock at +0.3R → scale-in at +0.5R
    → partial close at +0.6R → trailing stop after partial → SL hit → exit.
    """
    tm = TradeManager(db_session, setup["bot_id"], setup["user_id"], "1m")

    # 1) Open cohort
    cohort = tm.open_cohort(
        instrument="BTCUSD", side="buy",
        entry_price=80000.0, atr=400.0, qty=0.01,
        stop_loss=79600.0, take_profit=81600.0,
    )
    db_session.commit()
    assert cohort.status == CohortStatus.open
    assert len(cohort.legs) == 1

    # 2) Price moves to +0.3R (80120) — breakeven lock fires
    cmd = tm.evaluate(cohort, current_price=80120.0,
                      forecast_drift=0.5, forecast_confidence=2.0)
    assert cmd is not None
    assert cmd.kind == "modify_sl"
    assert cmd.reason == "breakeven_lock"
    tm.update_stop(cohort, cmd.new_stop)
    db_session.commit()
    assert cohort.current_stop == 80000.0  # break-even

    # 3) Price moves to +0.5R (80200) — scale-in fires (BE is done, SL won't
    #    move again unless price keeps going up)
    cmd = tm.evaluate(cohort, current_price=80200.0,
                      forecast_drift=0.5, forecast_confidence=2.0)
    assert cmd is not None
    assert cmd.kind == "scale_in"
    tm.add_scale_in_leg(
        cohort, entry_price=80200.0, qty=cmd.qty, stop_loss=cmd.new_stop,
    )
    db_session.commit()
    assert len(cohort.legs) == 2

    # 4) Price moves to +0.6R from new weighted_avg (which is now 80100).
    #    0.6R off 80100 = 80100 + 0.6*400 = 80340. BE re-fires on the new avg.
    cmd = tm.evaluate(cohort, current_price=80340.0,
                      forecast_drift=0.5, forecast_confidence=2.0)
    assert cmd is not None and cmd.kind == "modify_sl"
    tm.update_stop(cohort, cmd.new_stop)
    db_session.commit()

    # 5) Same price → now partial close fires
    cmd = tm.evaluate(cohort, current_price=80340.0,
                      forecast_drift=0.5, forecast_confidence=2.0)
    assert cmd is not None and cmd.kind == "partial_close"
    tm.record_partial_close(
        cohort, qty_closed=cmd.qty, close_price=80340.0,
    )
    tm.update_stop(cohort, cmd.new_stop or cohort.weighted_avg_entry)
    db_session.commit()
    assert cohort.status == CohortStatus.partial

    # 6) Price reverses and hits SL → exit_all
    cmd = tm.evaluate(cohort, current_price=cohort.current_stop - 1,
                      forecast_drift=0.0, forecast_confidence=0.0)
    assert cmd is not None and cmd.kind == "exit_all"
    tm.close_cohort(cohort, close_price=cohort.current_stop - 1, reason=cmd.reason)
    db_session.commit()

    # 7) Verify final state
    assert cohort.status == CohortStatus.closed
    for leg in cohort.legs:
        assert leg.is_open is False

    # TradeOutcome row created with the realized PnL
    outcomes = (
        db_session.query(TradeOutcome)
        .filter(TradeOutcome.bot_id == setup["bot_id"])
        .all()
    )
    assert len(outcomes) >= 1


@pytest.mark.asyncio
async def test_full_sell_lifecycle_exit_at_tp(db_session, setup):
    """Mirror lifecycle but on the short side, exiting at TP."""
    tm = TradeManager(db_session, setup["bot_id"], setup["user_id"], "1m")
    cohort = tm.open_cohort(
        instrument="BTCUSD", side="sell",
        entry_price=80000.0, atr=400.0, qty=0.01,
        stop_loss=80400.0, take_profit=78400.0,  # 4R below
    )
    db_session.commit()

    # Move price DOWN (favorable for short) to TP
    cmd = tm.evaluate(cohort, current_price=78400.0,
                      forecast_drift=-0.5, forecast_confidence=2.0)
    assert cmd is not None and cmd.kind == "exit_all"
    assert cmd.reason == "tp_final_hit"
    tm.close_cohort(cohort, close_price=78400.0, reason=cmd.reason)
    db_session.commit()
    assert cohort.status == CohortStatus.closed


@pytest.mark.asyncio
async def test_lifecycle_exits_on_forecast_reversal(db_session, setup):
    """Forecast flips with >1.0σ confidence → exit_all even if in profit."""
    tm = TradeManager(db_session, setup["bot_id"], setup["user_id"], "1m")
    cohort = tm.open_cohort(
        instrument="BTCUSD", side="buy",
        entry_price=80000.0, atr=400.0, qty=0.01,
        stop_loss=79600.0, take_profit=82000.0,
    )
    db_session.commit()

    # Move +0.4R favorable but forecast says sell
    cmd = tm.evaluate(cohort, current_price=80160.0,
                      forecast_drift=-0.5, forecast_confidence=2.0)
    assert cmd is not None and cmd.kind == "exit_all"
    assert cmd.reason == "forecast_reversed"


@pytest.mark.asyncio
async def test_lifecycle_exits_on_drawdown_breach(db_session, setup):
    """Price moves >1.5×ATR against → exit_all (drawdown_breach)."""
    tm = TradeManager(db_session, setup["bot_id"], setup["user_id"], "1m")
    cohort = tm.open_cohort(
        instrument="BTCUSD", side="buy",
        entry_price=80000.0, atr=400.0, qty=0.01,
        stop_loss=79000.0, take_profit=82000.0,  # SL = -2.5R (deeper than DD limit)
    )
    db_session.commit()

    # Price drops 1.6×ATR = -640 → 79360
    cmd = tm.evaluate(cohort, current_price=79360.0,
                      forecast_drift=0.0, forecast_confidence=0.0)
    assert cmd is not None and cmd.kind == "exit_all"
    assert cmd.reason == "drawdown_breach"


# ---------- MockBroker integration ----------

@pytest.mark.asyncio
async def test_mock_broker_records_full_lifecycle():
    """Driving MockBroker through entry → modify → partial_close → close
    captures the contract surface the real broker is expected to support.
    """
    broker = MockBroker()
    broker.set_quote("BTCUSD", 80000, 80020)

    # 1) Open
    r = await broker.place_order(
        account_id="1", token="t", acc_num="1",
        symbol="BTCUSD", side="buy", qty=0.01,
        sl=79900, tp=80200, client_order_id="entry-1",
    )
    assert r["duplicate"] is False
    pos_id = (await broker.get_positions("1", "t", "1"))[0]["id"]

    # 2) Modify SL
    await broker.modify_position(pos_id, token="t", acc_num="1", stop_loss=80000)
    pos = (await broker.get_positions("1", "t", "1"))[0]
    assert pos["stopLoss"] == 80000

    # 3) Partial close (hedging mode: creates opposite-side leg)
    await broker.partial_close(
        account_id="1", token="t", acc_num="1",
        position_id=pos_id, symbol="BTCUSD",
        original_side="buy", qty=0.005,
    )
    assert broker.orders_placed == 2  # entry + partial-close (opposite-side market)

    # 4) Close the rest
    await broker.close_position(pos_id, token="t", acc_num="1")
    open_pos = await broker.get_positions("1", "t", "1")
    assert all(p["id"] != pos_id for p in open_pos)


@pytest.mark.asyncio
async def test_mock_broker_dedups_repeated_coid():
    """Idempotency contract from production client must work here too."""
    broker = MockBroker()
    broker.set_quote("BTCUSD", 80000, 80020)
    r1 = await broker.place_order(
        account_id="1", token="t", acc_num="1",
        symbol="BTCUSD", side="buy", qty=0.01,
        client_order_id="dedup-test",
    )
    r2 = await broker.place_order(
        account_id="1", token="t", acc_num="1",
        symbol="BTCUSD", side="buy", qty=0.01,
        client_order_id="dedup-test",
    )
    assert r2["duplicate"] is True
    assert r1["order_id"] == r2["order_id"]
    assert broker.orders_placed == 1  # only ONE actual order recorded
