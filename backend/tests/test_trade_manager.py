"""Unit tests for TradeManager — cohort lifecycle, weighted-avg math, decisions."""
from __future__ import annotations

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
