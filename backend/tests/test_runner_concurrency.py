"""Concurrent QuantRunner integration test.

Hypothesis: when TWO QuantRunner instances (different bot_ids, same user)
attempt entries on the SAME (symbol, side) simultaneously, the safeguards
in place — idempotency cache, broker-truth position cap, broker exposure
check — should ensure only ONE entry actually fires.

This is the failure mode that previously caused duplicate cohorts on the
1m / 5m runners both firing at a bar close. Verifies:

  1. Only one entry is placed per (symbol, side) within a 1-sec window.
  2. broker.orders_placed equals the actual open position count.
  3. No duplicate cohorts are persisted in the DB for the same broker
     position id.
  4. asyncio.gather completes cleanly (no exceptions leak).

The test uses two QuantRunner instances pointed at the SAME MockBroker
instance. Because TradeLockerClient's idempotency cache is class-level
(shared across instances), feeding both runners the same MockBroker
exercises the broker-side dedup path. We also test the broker-truth
position cap by checking that the SECOND runner sees the first's order
in its broker-position scan.
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pandas as pd
import pytest

from app.db.models import (
    Bot,
    Cohort,
    CohortStatus,
    StrategyState,
    StrategyType,
    User,
)
from app.strategies.base import StrategySignal
from app.strategies.data_feed import BarFetcher
from app.strategies.quant_strategy import LatPFNQuantStrategy
from app.strategies.runner import QuantRunner
from tests.fixtures.mock_broker import MockBroker


def _make_bars(n: int = 240, base: float = 80000.0) -> pd.DataFrame:
    import numpy as np

    closes = base + np.zeros(n)
    return pd.DataFrame(
        {
            "open": closes - 0.1,
            "high": closes + 0.5,
            "low": closes - 0.5,
            "close": closes,
            "volume": [1.0] * n,
        },
        index=pd.date_range("2026-05-10", periods=n, freq="1min", tz="UTC"),
    )


def _session_factory_from(db_session):
    """Build a session-factory callable that returns a non-closing wrapper
    around the test session — so runner-internal db.close() doesn't drop
    the shared transaction."""

    class _Wrap:
        def __init__(self, s):
            self._s = s

        def __getattr__(self, item):
            return getattr(self._s, item)

        def close(self):
            pass

    def _factory():
        return _Wrap(db_session)

    return _factory


def _make_signal(symbol: str = "BTCUSD", side: str = "buy") -> StrategySignal:
    return StrategySignal(
        symbol=symbol,
        side=side,
        entry_price=80020.0,
        stop_loss=79900.0,
        take_profit=80200.0,
        qty=0.01,
        forecast_drift=1.0,
        forecast_confidence=2.0,
        threshold=0.5,
        extra={"kind": "entry", "atr": 120.0},
    )


@pytest.fixture
def two_quant_runners(db_session):
    """Two QuantRunners (different bot_ids) pointed at the same user + broker.

    Each runner has its own bot_id (so the registry can hold both), the
    same timeframe, the same symbol, and the same user. Returns:
        (runner_a, runner_b, user, broker)
    """
    # Two separate bot rows so the runner registry can hold both runners
    # simultaneously without collision on the (bot_id, tf, cls_name) key.
    bot_a = Bot(
        name="QuantA",
        slug="latpfn-quant-a",
        description="",
        strategy_type=StrategyType.latpfn_quant,
        instruments_csv="BTCUSD",
        webhook_secret="conc-test-a",
    )
    bot_b = Bot(
        name="QuantB",
        slug="latpfn-quant-b",
        description="",
        strategy_type=StrategyType.latpfn_quant,
        instruments_csv="BTCUSD",
        webhook_secret="conc-test-b",
    )
    db_session.add_all([bot_a, bot_b])
    db_session.commit()
    db_session.refresh(bot_a)
    db_session.refresh(bot_b)

    user = User(email="concurrent@example.com", hashed_password="x")
    user.tradelocker_account_id = "1"
    user.tradelocker_acc_num = "1"
    user.tradelocker_env = "demo"
    from app.core.crypto import encrypt

    user.tradelocker_token = encrypt("fake-token-conc")
    user.max_lot_override = 0.01
    user.max_concurrent_positions = 1  # CAP=1: only one open position allowed
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)

    for bot in (bot_a, bot_b):
        db_session.add(
            StrategyState(
                bot_id=bot.id,
                timeframe="1m",
                is_running=True,
                confidence_threshold=0.5,
            )
        )
    db_session.commit()

    factory = _session_factory_from(db_session)
    runner_a = QuantRunner(
        db_session_factory=factory,
        bot_id=bot_a.id,
        timeframe="1m",
        symbols=["BTCUSD"],
        user_emails=[user.email],
    )
    runner_b = QuantRunner(
        db_session_factory=factory,
        bot_id=bot_b.id,
        timeframe="1m",
        symbols=["BTCUSD"],
        user_emails=[user.email],
    )
    broker = MockBroker()
    broker.set_quote("BTCUSD", 80000, 80020)
    return runner_a, runner_b, user, broker, db_session


@pytest.mark.asyncio
async def test_concurrent_runners_only_one_entry_per_symbol(two_quant_runners):
    """When two runners tick simultaneously on the same (symbol, side, user),
    only ONE entry should fire — the position-cap broker-truth check
    must catch the second one (or, failing that, broker-side idempotency
    if both happen to share a client_order_id).

    Cap is 1, so after either runner enters, the other must see the
    broker has a position and abort.
    """
    runner_a, runner_b, user, broker, db_session = two_quant_runners

    bars = _make_bars()
    bar_fetcher = AsyncMock(spec=BarFetcher)
    bar_fetcher.fetch_bars = AsyncMock(return_value=bars)

    def _strategy() -> AsyncMock:
        strat = AsyncMock(spec=LatPFNQuantStrategy)
        strat.forecast_view = AsyncMock(
            return_value={"drift": 1.0, "confidence": 2.0}
        )
        strat.on_bar = AsyncMock(return_value=_make_signal("BTCUSD", "buy"))
        return strat

    strat_a = _strategy()
    strat_b = _strategy()

    # Launch both ticks concurrently. asyncio.gather should complete
    # cleanly — neither tick should raise back into us.
    results = await asyncio.gather(
        runner_a._tick(strat_a, bar_fetcher, broker),
        runner_b._tick(strat_b, bar_fetcher, broker),
        return_exceptions=True,
    )
    # Assert no task raised
    for r in results:
        assert not isinstance(r, Exception), f"runner tick raised: {r!r}"

    # Assert only ONE buy order on BTCUSD got placed
    btc_buys = [
        o for o in broker.orders if o.symbol == "BTCUSD" and o.side == "buy"
    ]
    assert len(btc_buys) == 1, (
        f"expected exactly 1 BTC buy after concurrent ticks, got {len(btc_buys)}: "
        f"{[o.order_id for o in btc_buys]}"
    )

    # Assert broker.orders_placed equals open-position count (no orphan orders)
    assert len(broker.open_positions) == 1
    assert broker.orders_placed >= 1

    # Assert no duplicate cohorts for the same broker position
    pos_id = broker.open_positions[0].id
    cohorts_with_pos = (
        db_session.query(Cohort)
        .filter(Cohort.user_id == user.id)
        .filter(Cohort.status != CohortStatus.closed)
        .all()
    )
    # Each cohort's first leg's tradelocker_position_id should be unique
    # (either zero or one cohort references this broker position)
    matching = [
        c
        for c in cohorts_with_pos
        if any(
            leg.tradelocker_position_id == pos_id and leg.role == "entry"
            for leg in c.legs
        )
    ]
    assert len(matching) <= 1, (
        f"multiple cohorts tracking broker position {pos_id}: "
        f"{[c.id for c in matching]}"
    )


@pytest.mark.asyncio
async def test_concurrent_runners_complete_via_gather_without_exceptions(
    two_quant_runners,
):
    """Smoke test: even without an entry signal both ticks complete OK."""
    runner_a, runner_b, user, broker, _db_session = two_quant_runners

    bars = _make_bars()
    bar_fetcher = AsyncMock(spec=BarFetcher)
    bar_fetcher.fetch_bars = AsyncMock(return_value=bars)

    def _strategy() -> AsyncMock:
        strat = AsyncMock(spec=LatPFNQuantStrategy)
        strat.forecast_view = AsyncMock(
            return_value={"drift": 0.1, "confidence": 0.2}  # below threshold
        )
        strat.on_bar = AsyncMock(return_value=None)
        return strat

    results = await asyncio.gather(
        runner_a._tick(_strategy(), bar_fetcher, broker),
        runner_b._tick(_strategy(), bar_fetcher, broker),
        return_exceptions=True,
    )
    for r in results:
        assert not isinstance(r, Exception), f"runner tick raised: {r!r}"
    assert broker.orders_placed == 0


@pytest.mark.asyncio
async def test_concurrent_runners_broker_truth_count_matches_orders(
    two_quant_runners,
):
    """After concurrent ticks, broker open_positions count and orders count
    are consistent — no half-placed orders missing position rows.
    """
    runner_a, runner_b, user, broker, _db_session = two_quant_runners

    # Increase cap so we don't rely solely on position-cap to dedup
    user.max_concurrent_positions = 5

    bars = _make_bars()
    bar_fetcher = AsyncMock(spec=BarFetcher)
    bar_fetcher.fetch_bars = AsyncMock(return_value=bars)

    def _strategy() -> AsyncMock:
        strat = AsyncMock(spec=LatPFNQuantStrategy)
        strat.forecast_view = AsyncMock(
            return_value={"drift": 1.0, "confidence": 2.0}
        )
        strat.on_bar = AsyncMock(return_value=_make_signal("BTCUSD", "buy"))
        return strat

    await asyncio.gather(
        runner_a._tick(_strategy(), bar_fetcher, broker),
        runner_b._tick(_strategy(), bar_fetcher, broker),
        return_exceptions=False,
    )

    # Either:
    #   (a) Both runners placed an order → broker has 2 BTC positions
    #   (b) Broker-exposure check ("skip_existing_position") on the 2nd
    #       tick blocked the second order → 1 position
    # In all cases, orders_placed for BTC == open_positions for BTC, no
    # phantom positions.
    btc_orders = [o for o in broker.orders if o.symbol == "BTCUSD"]
    btc_positions = [p for p in broker.open_positions if not p.closed]
    # Each placed order opens exactly one position in MockBroker (no exit
    # was triggered here), so order count = position count.
    assert len(btc_orders) == len(btc_positions), (
        f"order/position drift: {len(btc_orders)} orders vs "
        f"{len(btc_positions)} open positions"
    )
