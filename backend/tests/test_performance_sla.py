"""Performance SLA regression tests.

These guard against accidental slowdown on hot-path code that runs every
tick. Each test:

  1. Warms up with 5 calls (avoid first-call cold cache / JIT effects)
  2. Measures wall-clock time of 50 calls via time.perf_counter()
  3. Asserts median runtime < target SLA

Markers:
  @pytest.mark.perf — skip with `-m "not perf"` on slow CI runners.

SLA budget (median, single-call):
  compute_rolling_stats (1000 trades) : 100 ms
  compute_rsi (240 bars)              :  10 ms
  passes_no_chase (240 bars)          :   5 ms
  TradeManager.evaluate               :   5 ms
  _idempotency_cache_hit lookup       :   0.1 ms

These budgets are 10× headroom over local M1 measurements so CI runners
with cold caches still pass.
"""
from __future__ import annotations

import statistics
import time
from datetime import datetime

import numpy as np
import pandas as pd
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.db.models  # noqa: F401  — register on Base
from app.core.tradelocker_client import TradeLockerClient
from app.db.database import Base
from app.db.models import Bot, StrategyType, TradeOutcome, User
from app.strategies.exhaustion_filter import compute_rsi, passes_no_chase
from app.strategies.performance_tracker import PerformanceTracker
from app.strategies.trade_manager import TradeManager


pytestmark = pytest.mark.perf


# ---------- helpers ----------


def _median_us(fn, iters: int = 50, warmup: int = 5) -> float:
    """Run fn() `iters` times, return median microseconds."""
    for _ in range(warmup):
        fn()
    samples = []
    for _ in range(iters):
        t0 = time.perf_counter()
        fn()
        samples.append((time.perf_counter() - t0) * 1e6)
    return statistics.median(samples)


def _make_bars(n: int = 240, base: float = 80000.0) -> pd.DataFrame:
    """A realistic 240-bar OHLC frame (1m, btc-ish)."""
    rng = np.random.default_rng(seed=42)
    drift = rng.normal(0.0, 5.0, size=n).cumsum()
    closes = base + drift
    opens = closes - rng.uniform(-2.0, 2.0, size=n)
    highs = np.maximum(closes, opens) + rng.uniform(0.5, 3.0, size=n)
    lows = np.minimum(closes, opens) - rng.uniform(0.5, 3.0, size=n)
    idx = pd.date_range("2026-05-10", periods=n, freq="1min", tz="UTC")
    return pd.DataFrame(
        {
            "open": opens,
            "high": highs,
            "low": lows,
            "close": closes,
            "volume": np.full(n, 1.0),
        },
        index=idx,
    )


# ---------- fixtures (shared inside this module) ----------


@pytest.fixture(scope="module")
def perf_db():
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
    Base.metadata.drop_all(bind=engine)
    engine.dispose()


@pytest.fixture(scope="module")
def perf_bot(perf_db):
    bot = Bot(
        name="PerfBot",
        slug="perf-bot",
        description="",
        strategy_type=StrategyType.latpfn_quant,
        instruments_csv="BTCUSD",
        webhook_secret="perf-test",
    )
    perf_db.add(bot)
    perf_db.commit()
    perf_db.refresh(bot)
    return bot


@pytest.fixture(scope="module")
def perf_trades(perf_db, perf_bot):
    """Seed 1000 closed TradeOutcome rows for performance stats benchmarks."""
    rng = np.random.default_rng(42)
    base_time = datetime(2026, 1, 1)
    rows = []
    for i in range(1000):
        r_mult = float(rng.normal(0.2, 1.5))
        pnl = r_mult * 50.0
        rows.append(
            TradeOutcome(
                bot_id=perf_bot.id,
                instrument="BTCUSD",
                side="buy" if i % 2 == 0 else "sell",
                timeframe="1m",
                entry_price=80000.0 + i,
                exit_price=80000.0 + i + (r_mult * 10),
                qty=0.01,
                pnl_usd=pnl,
                r_multiple=r_mult,
                opened_at=base_time,
                closed_at=base_time,
                hold_seconds=300,
            )
        )
    perf_db.add_all(rows)
    perf_db.commit()
    return rows


# ---------- SLA: compute_rolling_stats ----------


def test_sla_compute_rolling_stats_1000_trades(perf_db, perf_bot, perf_trades):
    """Stats over 1000 closed trades < 100 ms median."""
    tracker = PerformanceTracker(perf_db, perf_bot.id)

    def _call():
        tracker.compute_rolling_stats(window=1000)

    median = _median_us(_call, iters=50, warmup=5)
    assert median < 100_000, f"compute_rolling_stats took {median:.0f}us median (SLA 100ms)"


# ---------- SLA: compute_rsi ----------


def test_sla_compute_rsi_240_bars():
    """RSI over 240 closes < 10 ms median."""
    closes = _make_bars(240)["close"].to_numpy(dtype=float)

    def _call():
        compute_rsi(closes, period=14)

    median = _median_us(_call, iters=50, warmup=5)
    assert median < 10_000, f"compute_rsi took {median:.0f}us median (SLA 10ms)"


# ---------- SLA: passes_no_chase ----------


def test_sla_passes_no_chase_240_bars():
    """no-chase filter over 240 bars < 5 ms median."""
    bars = _make_bars(240)

    def _call():
        passes_no_chase(bars, side="buy")

    median = _median_us(_call, iters=50, warmup=5)
    assert median < 5_000, f"passes_no_chase took {median:.0f}us median (SLA 5ms)"


# ---------- SLA: TradeManager.evaluate ----------


def test_sla_trade_manager_evaluate(perf_db, perf_bot):
    """Single TradeManager.evaluate() call < 5 ms median."""
    # Create a user + a cohort to evaluate
    user = User(email="perf-eval@example.com", hashed_password="x")
    perf_db.add(user)
    perf_db.commit()
    perf_db.refresh(user)

    tm = TradeManager(perf_db, perf_bot.id, user.id, "1m")
    cohort = tm.open_cohort(
        instrument="BTCUSD",
        side="buy",
        entry_price=80000.0,
        atr=400.0,
        qty=0.02,
        stop_loss=79600.0,
        take_profit=81200.0,
    )
    perf_db.commit()
    # Mid-favorable move
    price = 80100.0

    def _call():
        tm.evaluate(
            cohort,
            current_price=price,
            forecast_drift=0.0,
            forecast_confidence=0.0,
        )

    median = _median_us(_call, iters=50, warmup=5)
    assert median < 5_000, f"TradeManager.evaluate took {median:.0f}us median (SLA 5ms)"


# ---------- SLA: _idempotency_cache_hit lookup ----------


def test_sla_idempotency_cache_lookup():
    """Hot-path cache hit lookup < 0.1 ms (100us) median."""
    client = TradeLockerClient(env="demo")
    key = "perf-test-key-001"
    client._idempotency_cache_put(key, {"order_id": "MOCK-1", "duplicate": False})

    def _call():
        client._idempotency_cache_hit(key)

    median = _median_us(_call, iters=50, warmup=5)
    assert median < 100, f"idempotency cache lookup took {median:.2f}us median (SLA 100us)"

    # Clean up so we don't leak into other tests using TradeLockerClient
    client._idempotency_cache.pop(key, None)
