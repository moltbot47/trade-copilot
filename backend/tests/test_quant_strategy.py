"""Tests for LatPFNQuantStrategy + state-machine transitions through TradeManager."""
from __future__ import annotations

from unittest.mock import AsyncMock

import numpy as np
import pandas as pd
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.database import Base
import app.db.models  # noqa: F401
from app.db.models import Bot, CohortStatus, StrategyType, User
from app.strategies.quant_strategy import LatPFNQuantStrategy
from app.strategies.trade_manager import TradeManager


def _make_bars(n: int = 30, base: float = 80000.0, noise: float = 50.0) -> pd.DataFrame:
    rng = np.random.default_rng(42)
    closes = base + rng.normal(0, noise, n).cumsum()
    return pd.DataFrame(
        {
            "open": closes,
            "high": closes + 30,
            "low": closes - 30,
            "close": closes,
            "volume": np.full(n, 1000.0),
        }
    )


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
        name="Quant", slug="latpfn-quant", description="",
        strategy_type=StrategyType.latpfn_quant,
        instruments_csv="BTCUSD", webhook_secret="x",
    )
    user = User(
        email="t@x.com", hashed_password="h",
        tradelocker_acc_num="4", tradelocker_account_id="2163244",
    )
    session.add_all([bot, user])
    session.commit()
    session.refresh(bot)
    session.refresh(user)
    return {"bot_id": bot.id, "user_id": user.id}


def _mock_latpfn_client(drift_endpoint: float, std_value: float = 25.0, current: float = 80000.0):
    """Build a fake LatPFN client whose forecast() returns a predictable mean/std."""
    from app.strategies.latpfn_client import LaTPFNClient

    client = LaTPFNClient(endpoint_url=None)
    horizon = 12
    # mean is monotonic from current → current+drift_endpoint over horizon steps
    mean = np.linspace(current + drift_endpoint * 0.1, current + drift_endpoint, horizon).tolist()
    std = [std_value] * horizon
    client.forecast = AsyncMock(  # type: ignore[method-assign]
        return_value={"mean": mean, "std": std, "current_price": current}
    )
    return client


@pytest.mark.asyncio
async def test_strategy_emits_buy_when_drift_positive(setup):
    bars = _make_bars(30, base=80000, noise=20)
    cur = float(bars["close"].iloc[-1])
    client = _mock_latpfn_client(drift_endpoint=400.0, std_value=30.0, current=cur)
    s = LatPFNQuantStrategy(
        bot_id=setup["bot_id"], timeframe="1m", latpfn_client=client, threshold=1.0
    )
    sig = await s.on_bar("BTCUSD", bars)
    assert sig is not None
    assert sig.side == "buy"
    assert sig.extra["kind"] == "entry"
    assert sig.extra["strategy"] == "latpfn_quant"
    assert sig.stop_loss < sig.entry_price
    assert sig.take_profit > sig.entry_price


@pytest.mark.asyncio
async def test_strategy_emits_sell_when_drift_negative(setup):
    bars = _make_bars(30, base=80000, noise=20)
    cur = float(bars["close"].iloc[-1])
    client = _mock_latpfn_client(drift_endpoint=-400.0, std_value=30.0, current=cur)
    s = LatPFNQuantStrategy(
        bot_id=setup["bot_id"], timeframe="1m", latpfn_client=client, threshold=1.0
    )
    sig = await s.on_bar("BTCUSD", bars)
    assert sig is not None
    assert sig.side == "sell"
    assert sig.stop_loss > sig.entry_price
    assert sig.take_profit < sig.entry_price


@pytest.mark.asyncio
async def test_strategy_no_signal_when_below_threshold(setup):
    bars = _make_bars(30, base=80000, noise=20)
    cur = float(bars["close"].iloc[-1])
    client = _mock_latpfn_client(drift_endpoint=10.0, std_value=200.0, current=cur)
    s = LatPFNQuantStrategy(
        bot_id=setup["bot_id"], timeframe="1m", latpfn_client=client, threshold=1.5
    )
    sig = await s.on_bar("BTCUSD", bars)
    assert sig is None


@pytest.mark.asyncio
async def test_strategy_returns_none_on_short_bars(setup):
    bars = _make_bars(5)
    client = _mock_latpfn_client(drift_endpoint=400.0)
    s = LatPFNQuantStrategy(setup["bot_id"], "1m", client)
    sig = await s.on_bar("BTCUSD", bars)
    assert sig is None


@pytest.mark.asyncio
async def test_full_lifecycle_state_machine(session, setup):
    """Synthetic price walk drives entry → scale-in → partial-close → trail → exit."""
    tm = TradeManager(session, setup["bot_id"], setup["user_id"], "1m")
    # Open initial cohort
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
    # Step 1: +0.5R → scale-in
    cmd1 = tm.evaluate(c, current_price=80200.0, forecast_drift=0.5, forecast_confidence=2.0)
    assert cmd1 is not None and cmd1.kind == "scale_in"
    tm.add_scale_in_leg(c, entry_price=80200.0, qty=cmd1.qty, stop_loss=cmd1.new_stop)
    session.commit()
    assert len(c.legs) == 2
    assert c.weighted_avg_entry == pytest.approx(80100.0)

    # Step 2: +1R from new avg (80100 + 400 = 80500) → partial close
    cmd2 = tm.evaluate(c, current_price=80500.0, forecast_drift=0.5, forecast_confidence=2.0)
    assert cmd2 is not None and cmd2.kind == "partial_close"
    tm.record_partial_close(c, qty_closed=cmd2.qty, close_price=80500.0)
    tm.update_stop(c, cmd2.new_stop or 80100.0)
    session.commit()
    assert c.status == CohortStatus.partial

    # Step 3: price moves further favorable → trailing SL
    cmd3 = tm.evaluate(c, current_price=80800.0, forecast_drift=0.5, forecast_confidence=2.0)
    assert cmd3 is not None and cmd3.kind == "modify_sl"
    assert cmd3.new_stop > c.current_stop
    tm.update_stop(c, cmd3.new_stop)
    session.commit()

    # Step 4: SL hit → exit_all
    cmd4 = tm.evaluate(c, current_price=c.current_stop - 1, forecast_drift=0.0, forecast_confidence=0.0)
    assert cmd4 is not None and cmd4.kind == "exit_all"
    tm.close_cohort(c, close_price=c.current_stop - 1, reason=cmd4.reason)
    session.commit()
    assert c.status == CohortStatus.closed


@pytest.mark.asyncio
async def test_no_double_scale_in(session, setup):
    """After scaling in once, last_action == 'scale_in' should suppress further scale-ins
    until other state transitions occur."""
    tm = TradeManager(session, setup["bot_id"], setup["user_id"], "1m")
    c = tm.open_cohort(
        instrument="BTCUSD", side="buy",
        entry_price=80000.0, atr=400.0, qty=0.02,
        stop_loss=79600.0, take_profit=82000.0,
    )
    session.commit()
    cmd = tm.evaluate(c, current_price=80200.0, forecast_drift=0.5, forecast_confidence=2.0)
    assert cmd.kind == "scale_in"
    tm.add_scale_in_leg(c, entry_price=80200.0, qty=cmd.qty, stop_loss=cmd.new_stop)
    session.commit()
    # Re-evaluate at the same +0.5R — should NOT emit a second scale-in
    cmd2 = tm.evaluate(c, current_price=80250.0, forecast_drift=0.5, forecast_confidence=2.0)
    assert cmd2 is None or cmd2.kind != "scale_in"
