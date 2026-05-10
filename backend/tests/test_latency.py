"""Tests for app.monitoring.latency."""
from __future__ import annotations

import asyncio
import time

import pytest

from app.monitoring.latency import (
    LatencyTracker,
    get_latency_tracker,
    reset_latency_tracker,
)


@pytest.fixture(autouse=True)
def _reset_singleton():
    reset_latency_tracker()
    yield
    reset_latency_tracker()


def test_record_and_summary_roundtrip():
    tracker = LatencyTracker()
    for ms in (10.0, 20.0, 30.0):
        tracker.record_nowait("forecast", ms)
    s = tracker.summary("forecast")
    assert s["count"] == 3
    assert s["max"] == 30.0
    assert s["mean"] == pytest.approx(20.0)


def test_summary_empty_stage_returns_zeros():
    tracker = LatencyTracker()
    s = tracker.summary("nonexistent")
    assert s == {"count": 0, "p50": 0.0, "p95": 0.0, "p99": 0.0, "max": 0.0, "mean": 0.0}


def test_percentile_math_known_input():
    """For [10,20,...,100], nearest-rank: p50≈50, p95≈100, p99≈100."""
    tracker = LatencyTracker()
    for ms in range(10, 101, 10):  # 10..100 step 10 → 10 samples
        tracker.record_nowait("forecast", float(ms))
    s = tracker.summary("forecast")
    assert s["count"] == 10
    # Nearest-rank with 10 sorted samples [10..100]:
    #   p50 → index round(0.5*9)=4 → samples[4] = 50
    #   p95 → index round(0.95*9)=9 (rounded from 8.55) → samples[9] = 100
    #   p99 → index round(0.99*9)=9 → samples[9] = 100
    assert s["p50"] == 50.0
    assert s["p95"] == 100.0
    assert s["p99"] == 100.0
    assert s["max"] == 100.0
    assert s["mean"] == pytest.approx(55.0)


def test_rolling_window_evicts_oldest():
    tracker = LatencyTracker(max_samples=1000)
    for i in range(1500):
        tracker.record_nowait("forecast", float(i))
    s = tracker.summary("forecast")
    assert s["count"] == 1000
    # Oldest 500 were evicted, so min sample is now 500.0 — max is 1499.0.
    assert s["max"] == 1499.0


def test_summary_all_returns_all_stages():
    tracker = LatencyTracker()
    tracker.record_nowait("forecast", 12.0)
    tracker.record_nowait("place_order", 50.0)
    all_summaries = tracker.summary_all()
    assert set(all_summaries.keys()) == {"forecast", "place_order"}
    assert all_summaries["forecast"]["count"] == 1
    assert all_summaries["place_order"]["count"] == 1


def test_time_block_measures_elapsed():
    async def run() -> None:
        tracker = LatencyTracker()
        async with tracker.time_block("forecast"):
            await asyncio.sleep(0.02)
        s = tracker.summary("forecast")
        assert s["count"] == 1
        # Allow generous slack — CI VMs are noisy. We just want to confirm
        # the timer actually fired with a sane reading (>15ms, <500ms).
        assert 15.0 <= s["max"] < 500.0

    asyncio.run(run())


def test_time_block_records_even_on_exception():
    async def run() -> None:
        tracker = LatencyTracker()
        with pytest.raises(RuntimeError):
            async with tracker.time_block("forecast"):
                raise RuntimeError("boom")
        assert tracker.summary("forecast")["count"] == 1

    asyncio.run(run())


def test_concurrent_record_is_thread_safe_under_asyncio():
    async def run() -> None:
        tracker = LatencyTracker()
        await asyncio.gather(*[tracker.record("forecast", float(i)) for i in range(200)])
        assert tracker.summary("forecast")["count"] == 200

    asyncio.run(run())


def test_singleton_returns_same_instance():
    a = get_latency_tracker()
    b = get_latency_tracker()
    assert a is b


def test_singleton_reset_returns_new_instance():
    a = get_latency_tracker()
    reset_latency_tracker()
    b = get_latency_tracker()
    assert a is not b


def test_latency_endpoint_requires_auth(client):
    res = client.get("/api/metrics/latency")
    assert res.status_code == 401


def test_latency_endpoint_returns_summary(client, auth_headers):
    # Seed the singleton — record_nowait avoids the asyncio.Lock since
    # TestClient runs each request in its own loop.
    tracker = get_latency_tracker()
    tracker.record_nowait("forecast", 12.0)
    tracker.record_nowait("forecast", 25.0)
    tracker.record_nowait("place_order", 100.0)

    res = client.get("/api/metrics/latency", headers=auth_headers)
    assert res.status_code == 200
    body = res.json()
    assert set(body.keys()) == {"forecast", "place_order"}
    assert body["forecast"]["count"] == 2
    assert body["place_order"]["count"] == 1
