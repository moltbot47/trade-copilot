"""Unit tests for TradeManager — cohort lifecycle, weighted-avg math, decisions."""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.database import Base
import app.db.models  # noqa: F401
from app.db.models import Bot, CohortStatus, StrategyType, User
from app.strategies.trade_manager import TradeManager, weighted_average_entry, open_qty


@pytest.fixture()
def session():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine, autoflush=False, future=True)
    s = Session()
    yield s
    s.close()
    engine.dispose()


@pytest.fixture()
def setup(session):
    bot = Bot(
        name="Quant",
        slug="latpfn-quant",
        description="",
        strategy_type=StrategyType.latpfn_quant,
        instruments_csv="BTCUSD",
        webhook_secret="test-secret",
    )
    user = User(
        email="t@x.com",
        hashed_password="hash",
        tradelocker_acc_num="4",
        tradelocker_account_id="2163244",
    )
    session.add_all([bot, user])
    session.commit()
    session.refresh(bot)
    session.refresh(user)
    return {"bot_id": bot.id, "user_id": user.id}


def test_open_cohort_creates_initial_leg(session, setup):
    tm = TradeManager(session, setup["bot_id"], setup["user_id"], "1m")
    cohort = tm.open_cohort(
        instrument="BTCUSD",
        side="buy",
        entry_price=80000.0,
        atr=400.0,
        qty=0.02,
        stop_loss=79600.0,
        take_profit=81200.0,
    )
    session.commit()
    assert cohort.id is not None
    assert cohort.status == CohortStatus.open
    assert len(cohort.legs) == 1
    assert cohort.legs[0].role == "entry"
    assert cohort.weighted_avg_entry == 80000.0
    assert cohort.total_qty == 0.02


def test_weighted_avg_after_scale_in(session, setup):
    tm = TradeManager(session, setup["bot_id"], setup["user_id"], "1m")
    c = tm.open_cohort(
        instrument="BTCUSD",
        side="buy",
        entry_price=80000.0,
        atr=400.0,
        qty=0.02,
        stop_loss=79600.0,
        take_profit=81200.0,
    )
    session.commit()
    tm.add_scale_in_leg(c, entry_price=80200.0, qty=0.02, stop_loss=79800.0)
    session.commit()
    expected_avg = (80000.0 * 0.02 + 80200.0 * 0.02) / 0.04
    assert abs(c.weighted_avg_entry - expected_avg) < 1e-6
    assert c.total_qty == pytest.approx(0.04)
    assert weighted_average_entry(list(c.legs)) == pytest.approx(expected_avg)
    assert open_qty(list(c.legs)) == pytest.approx(0.04)


def test_evaluate_no_action_when_price_unchanged(session, setup):
    tm = TradeManager(session, setup["bot_id"], setup["user_id"], "1m")
    c = tm.open_cohort(
        instrument="BTCUSD",
        side="buy",
        entry_price=80000.0,
        atr=400.0,
        qty=0.02,
        stop_loss=79600.0,
        take_profit=81200.0,
    )
    session.commit()
    cmd = tm.evaluate(c, current_price=80000.0, forecast_drift=0.5, forecast_confidence=2.0)
    assert cmd is None


def test_evaluate_emits_scale_in_at_half_R_favorable(session, setup):
    tm = TradeManager(session, setup["bot_id"], setup["user_id"], "1m")
    c = tm.open_cohort(
        instrument="BTCUSD",
        side="buy",
        entry_price=80000.0,
        atr=400.0,
        qty=0.02,
        stop_loss=79600.0,
        take_profit=81200.0,
    )
    session.commit()
    # 1R = 400, +0.5R = +200 → price 80200
    cmd = tm.evaluate(c, current_price=80200.0, forecast_drift=0.6, forecast_confidence=2.0)
    assert cmd is not None
    assert cmd.kind == "scale_in"
    assert cmd.qty == pytest.approx(0.02)
    assert cmd.new_stop is not None and cmd.new_stop < 80200.0


def test_evaluate_blocks_scale_in_when_forecast_against(session, setup):
    tm = TradeManager(session, setup["bot_id"], setup["user_id"], "1m")
    c = tm.open_cohort(
        instrument="BTCUSD",
        side="buy",
        entry_price=80000.0,
        atr=400.0,
        qty=0.02,
        stop_loss=79600.0,
        take_profit=81200.0,
    )
    session.commit()
    # +0.5R favorable BUT forecast says down — no scale-in
    cmd = tm.evaluate(c, current_price=80200.0, forecast_drift=-0.6, forecast_confidence=2.0)
    assert cmd is None or cmd.kind != "scale_in"


def test_evaluate_emits_partial_close_at_1R(session, setup):
    tm = TradeManager(session, setup["bot_id"], setup["user_id"], "1m")
    c = tm.open_cohort(
        instrument="BTCUSD",
        side="buy",
        entry_price=80000.0,
        atr=400.0,
        qty=0.02,
        stop_loss=79600.0,
        take_profit=81600.0,  # 4R
    )
    session.commit()
    # +1R hit — price 80400
    cmd = tm.evaluate(c, current_price=80400.0, forecast_drift=0.6, forecast_confidence=2.0)
    assert cmd is not None
    assert cmd.kind == "partial_close"
    assert cmd.qty > 0
    assert cmd.new_stop == pytest.approx(c.weighted_avg_entry)  # break-even


def test_evaluate_trail_sl_after_partial(session, setup):
    tm = TradeManager(session, setup["bot_id"], setup["user_id"], "1m")
    c = tm.open_cohort(
        instrument="BTCUSD",
        side="buy",
        entry_price=80000.0,
        atr=400.0,
        qty=0.02,
        stop_loss=79600.0,
        take_profit=82000.0,
    )
    session.commit()
    # Force into partial state
    tm.record_partial_close(c, qty_closed=0.01, close_price=80400.0)
    tm.update_stop(c, 80000.0)  # break-even
    session.commit()
    # Price now well above (+2R) — trail SL should ratchet up
    cmd = tm.evaluate(c, current_price=80800.0, forecast_drift=0.5, forecast_confidence=2.0)
    assert cmd is not None
    assert cmd.kind == "modify_sl"
    # new stop should be > current_stop (80000)
    assert cmd.new_stop > 80000.0


def test_evaluate_exit_on_sl_hit(session, setup):
    tm = TradeManager(session, setup["bot_id"], setup["user_id"], "1m")
    c = tm.open_cohort(
        instrument="BTCUSD",
        side="buy",
        entry_price=80000.0,
        atr=400.0,
        qty=0.02,
        stop_loss=79600.0,
        take_profit=82000.0,
    )
    session.commit()
    cmd = tm.evaluate(c, current_price=79500.0, forecast_drift=0.0, forecast_confidence=0.0)
    assert cmd is not None
    assert cmd.kind == "exit_all"
    assert "sl" in cmd.reason


def test_evaluate_exit_on_tp_final(session, setup):
    tm = TradeManager(session, setup["bot_id"], setup["user_id"], "1m")
    c = tm.open_cohort(
        instrument="BTCUSD",
        side="buy",
        entry_price=80000.0,
        atr=400.0,
        qty=0.02,
        stop_loss=79600.0,
        take_profit=82000.0,
    )
    session.commit()
    cmd = tm.evaluate(c, current_price=82100.0, forecast_drift=0.5, forecast_confidence=2.0)
    assert cmd is not None
    assert cmd.kind == "exit_all"
    assert "tp_final" in cmd.reason


def test_evaluate_exit_on_drawdown_breach(session, setup):
    tm = TradeManager(session, setup["bot_id"], setup["user_id"], "1m")
    c = tm.open_cohort(
        instrument="BTCUSD",
        side="buy",
        entry_price=80000.0,
        atr=400.0,
        qty=0.02,
        stop_loss=78000.0,  # wide SL
        take_profit=82000.0,
    )
    session.commit()
    # price drops 1000 against avg = 2.5×ATR → breach, even though SL not hit
    cmd = tm.evaluate(c, current_price=79000.0, forecast_drift=0.0, forecast_confidence=0.0)
    assert cmd is not None
    assert cmd.kind == "exit_all"


def test_evaluate_exit_on_forecast_reversal(session, setup):
    tm = TradeManager(session, setup["bot_id"], setup["user_id"], "1m")
    c = tm.open_cohort(
        instrument="BTCUSD",
        side="buy",
        entry_price=80000.0,
        atr=400.0,
        qty=0.02,
        stop_loss=79600.0,
        take_profit=82000.0,
    )
    session.commit()
    # Price slightly favorable, but forecast reversed strongly
    cmd = tm.evaluate(c, current_price=80050.0, forecast_drift=-2.0, forecast_confidence=2.5)
    assert cmd is not None
    assert cmd.kind == "exit_all"
    assert "forecast" in cmd.reason


def test_close_cohort_marks_legs_closed(session, setup):
    tm = TradeManager(session, setup["bot_id"], setup["user_id"], "1m")
    c = tm.open_cohort(
        instrument="BTCUSD",
        side="buy",
        entry_price=80000.0,
        atr=400.0,
        qty=0.02,
        stop_loss=79600.0,
        take_profit=82000.0,
    )
    session.commit()
    tm.close_cohort(c, close_price=80800.0, reason="test_exit")
    session.commit()
    assert c.status == CohortStatus.closed
    assert c.closed_qty == pytest.approx(0.02)
    assert c.realized_pnl == pytest.approx((80800.0 - 80000.0) * 0.02)
    for leg in c.legs:
        assert leg.is_open is False


def test_short_side_evaluate_partial_close(session, setup):
    tm = TradeManager(session, setup["bot_id"], setup["user_id"], "1m")
    c = tm.open_cohort(
        instrument="BTCUSD",
        side="sell",
        entry_price=80000.0,
        atr=400.0,
        qty=0.02,
        stop_loss=80400.0,
        take_profit=78400.0,  # 4R below entry
    )
    session.commit()
    # +1R for short = price drops 400 → 79600
    cmd = tm.evaluate(c, current_price=79600.0, forecast_drift=-0.6, forecast_confidence=2.0)
    assert cmd is not None
    assert cmd.kind == "partial_close"


def _trailing_setup_buy(session, setup, *, ratchet_to_partial: bool = True):
    """Helper: open a long cohort and put it into partial/trailing state."""
    tm = TradeManager(session, setup["bot_id"], setup["user_id"], "1m")
    c = tm.open_cohort(
        instrument="BTCUSD",
        side="buy",
        entry_price=80000.0,
        atr=400.0,  # 1R = 400
        qty=0.04,
        stop_loss=79600.0,
        take_profit=84000.0,  # 10R away — leaves room for ratchet
        tl_position_id="POS_ENTRY",
    )
    session.commit()
    if ratchet_to_partial:
        tm.record_partial_close(c, qty_closed=0.02, close_price=80400.0)
        tm.update_stop(c, 80000.0)  # break-even
        for leg in c.legs:
            if leg.role == "entry":
                leg.stop_loss = 80000.0
                leg.tradelocker_position_id = "POS_ENTRY"
        session.commit()
    return tm, c


def test_ratchet_does_not_fire_at_exactly_1R(session, setup):
    """At +1R the ratchet trigger (1.5R) is NOT crossed — no broker call."""
    tm, c = _trailing_setup_buy(session, setup)
    mock_client = AsyncMock()
    tm.set_broker_context(mock_client, token="t", acc_num="1")

    updates = asyncio.run(tm.update_trailing_ratchet("BTCUSD", 80400.0))
    assert updates == []
    mock_client.modify_position.assert_not_called()


def test_ratchet_fires_at_1_5R_to_plus_0_5R(session, setup):
    """At +1.5R, SL should ratchet to +0.5R (entry + 0.5*R)."""
    tm, c = _trailing_setup_buy(session, setup)
    mock_client = AsyncMock()
    tm.set_broker_context(mock_client, token="t", acc_num="1")

    # +1.5R favorable (price 80600). target_sl_R = 0.5 → SL = 80000 + 0.5*400 = 80200
    updates = asyncio.run(tm.update_trailing_ratchet("BTCUSD", 80600.0))
    assert len(updates) == 1
    upd = updates[0]
    assert upd["new_sl"] == pytest.approx(80200.0)
    assert upd["favorable_R"] == pytest.approx(1.5)
    assert mock_client.modify_position.called
    call = mock_client.modify_position.call_args
    assert call.kwargs["stop_loss"] == pytest.approx(80200.0)
    assert call.kwargs["token"] == "t"
    assert call.kwargs["acc_num"] == "1"


def test_ratchet_fires_at_2R_to_plus_1R(session, setup):
    """At +2R, SL should ratchet to +1R (= weighted_avg + 1*R)."""
    tm, c = _trailing_setup_buy(session, setup)
    mock_client = AsyncMock()
    tm.set_broker_context(mock_client, token="t", acc_num="1")

    # +2R favorable (price 80800). target_sl_R = 1.0 → SL = 80000 + 1.0*400 = 80400
    updates = asyncio.run(tm.update_trailing_ratchet("BTCUSD", 80800.0))
    assert len(updates) == 1
    assert updates[0]["new_sl"] == pytest.approx(80400.0)
    assert updates[0]["favorable_R"] == pytest.approx(2.0)


def test_ratchet_holds_high_water_after_pullback(session, setup):
    """High of +2.5R locks SL at +1.5R; later +1.8R must NOT lower SL."""
    tm, c = _trailing_setup_buy(session, setup)
    mock_client = AsyncMock()
    tm.set_broker_context(mock_client, token="t", acc_num="1")

    # First push: +2.5R high (price = 80000 + 2.5*400 = 81000) → SL = +1.5R = 80600
    asyncio.run(tm.update_trailing_ratchet("BTCUSD", 81000.0))
    assert mock_client.modify_position.call_count == 1
    high_water_call = mock_client.modify_position.call_args
    assert high_water_call.kwargs["stop_loss"] == pytest.approx(80600.0)

    # Pullback to +1.8R (price = 80720) — SL must hold at 80600, no broker call
    updates = asyncio.run(tm.update_trailing_ratchet("BTCUSD", 80720.0))
    assert updates == []
    assert mock_client.modify_position.call_count == 1


def test_ratchet_short_side(session, setup):
    """Short cohort ratchet — verify symmetric math."""
    tm = TradeManager(session, setup["bot_id"], setup["user_id"], "1m")
    c = tm.open_cohort(
        instrument="BTCUSD",
        side="sell",
        entry_price=80000.0,
        atr=400.0,
        qty=0.04,
        stop_loss=80400.0,
        take_profit=76000.0,
        tl_position_id="POS_ENTRY_S",
    )
    session.commit()
    tm.record_partial_close(c, qty_closed=0.02, close_price=79600.0)
    tm.update_stop(c, 80000.0)
    for leg in c.legs:
        if leg.role == "entry":
            leg.stop_loss = 80000.0
            leg.tradelocker_position_id = "POS_ENTRY_S"
    session.commit()

    mock_client = AsyncMock()
    tm.set_broker_context(mock_client, token="t", acc_num="1")

    # +2R favorable for short = price drops 800 → 79200.
    # target_sl_R = 1.0 → SL = 80000 - 1.0*400 = 79600
    updates = asyncio.run(tm.update_trailing_ratchet("BTCUSD", 79200.0))
    assert len(updates) == 1
    assert updates[0]["new_sl"] == pytest.approx(79600.0)
    assert mock_client.modify_position.call_args.kwargs["stop_loss"] == pytest.approx(79600.0)


def test_ratchet_updates_each_leg_independently(session, setup):
    """A cohort with two open legs (entry + scale-in) ratchets both."""
    tm = TradeManager(session, setup["bot_id"], setup["user_id"], "1m")
    c = tm.open_cohort(
        instrument="BTCUSD",
        side="buy",
        entry_price=80000.0,
        atr=400.0,
        qty=0.02,
        stop_loss=79600.0,
        take_profit=84000.0,
        tl_position_id="POS_A",
    )
    session.commit()
    tm.add_scale_in_leg(
        c,
        entry_price=80000.0,
        qty=0.02,
        stop_loss=79600.0,
        tl_position_id="POS_B",
    )
    session.commit()
    tm.record_partial_close(c, qty_closed=0.02, close_price=80400.0)
    tm.update_stop(c, 80000.0)
    for leg in c.legs:
        if leg.role in ("entry", "scale_in"):
            leg.stop_loss = 80000.0
    session.commit()

    mock_client = AsyncMock()
    tm.set_broker_context(mock_client, token="t", acc_num="1")

    # +2R → SL = +1R = 80400 on every open entry/scale_in leg
    updates = asyncio.run(tm.update_trailing_ratchet("BTCUSD", 80800.0))
    assert len(updates) == 2
    assert mock_client.modify_position.call_count == 2
    pos_ids_called = {
        call.args[0] for call in mock_client.modify_position.call_args_list
    }
    assert pos_ids_called == {"POS_A", "POS_B"}
    for upd in updates:
        assert upd["new_sl"] == pytest.approx(80400.0)


def test_ratchet_skips_when_no_broker_context(session, setup):
    """Without client/token/acc_num, ratchet still updates DB but no broker call."""
    tm, c = _trailing_setup_buy(session, setup)
    updates = asyncio.run(tm.update_trailing_ratchet("BTCUSD", 80800.0))
    assert len(updates) == 1
    assert updates[0]["new_sl"] == pytest.approx(80400.0)


def test_ratchet_caps_at_max_R(session, setup):
    """Even at +10R, the ratchet target must not exceed the configured cap (5R)."""
    tm, c = _trailing_setup_buy(session, setup)
    mock_client = AsyncMock()
    tm.set_broker_context(mock_client, token="t", acc_num="1")

    # +10R favorable (price 84000) — without cap target_sl_R=9. With cap=5,
    # SL = entry + 5*R = 80000 + 5*400 = 82000.
    updates = asyncio.run(tm.update_trailing_ratchet("BTCUSD", 84000.0))
    assert len(updates) == 1
    assert updates[0]["new_sl"] == pytest.approx(82000.0)


def test_ratchet_only_runs_for_partial_cohorts(session, setup):
    """Cohort still in 'open' state must not trigger ratchet."""
    tm, c = _trailing_setup_buy(session, setup, ratchet_to_partial=False)
    mock_client = AsyncMock()
    tm.set_broker_context(mock_client, token="t", acc_num="1")

    updates = asyncio.run(tm.update_trailing_ratchet("BTCUSD", 81000.0))
    assert updates == []
    mock_client.modify_position.assert_not_called()


def test_list_open_cohorts_filters_by_status(session, setup):
    tm = TradeManager(session, setup["bot_id"], setup["user_id"], "1m")
    c1 = tm.open_cohort(
        instrument="BTCUSD", side="buy", entry_price=80000.0, atr=400.0,
        qty=0.02, stop_loss=79600.0, take_profit=81600.0,
    )
    c2 = tm.open_cohort(
        instrument="ETHUSD", side="buy", entry_price=3000.0, atr=10.0,
        qty=0.05, stop_loss=2990.0, take_profit=3030.0,
    )
    session.commit()
    tm.close_cohort(c2, close_price=3030.0, reason="test")
    session.commit()
    open_list = tm.list_open_cohorts()
    assert len(open_list) == 1
    assert open_list[0].id == c1.id
