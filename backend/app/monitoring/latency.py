"""In-process latency tracker with per-stage rolling percentile rollups.

Designed for the trading runner hot path: every call is a single deque
append guarded by an asyncio lock, and percentile math is computed lazily
on `summary()` reads (the dashboard polls infrequently relative to writes).

Wire-up points (NOT yet hooked into the runner — left intentionally so the
module can be reviewed in isolation):
  - `forecast`         — wrap `strategy.on_bar(bars)` in `_tick`
  - `place_order`      — wrap the broker call in `_open_new_cohort`
  - `position_lookup`  — wrap the post-fill position fetch right after
                         `place_order` returns

Sample integration:

    from app.monitoring.latency import get_latency_tracker

    latency = get_latency_tracker()
    async with latency.time_block("forecast"):
        signal = await strategy.on_bar(bars)
"""
from __future__ import annotations

import asyncio
import time
from collections import deque
from contextlib import asynccontextmanager
from typing import AsyncIterator

# 1000 samples is enough for stable p99 without unbounded growth in memory
# (deque of floats — ~24 KB per stage).
_MAX_SAMPLES = 1000


class LatencyTracker:
    def __init__(self, max_samples: int = _MAX_SAMPLES) -> None:
        self._max_samples = max_samples
        self._samples: dict[str, deque[float]] = {}
        # Single lock for the whole tracker — write critical section is a
        # deque.append, so contention is negligible vs. per-stage locks.
        self._lock = asyncio.Lock()

    async def record(self, stage: str, ms: float) -> None:
        async with self._lock:
            bucket = self._samples.get(stage)
            if bucket is None:
                bucket = deque(maxlen=self._max_samples)
                self._samples[stage] = bucket
            bucket.append(float(ms))

    def record_nowait(self, stage: str, ms: float) -> None:
        """Sync variant for callers that aren't in an event loop (e.g. tests).

        Skips the lock — only safe when no concurrent async writers exist.
        """
        bucket = self._samples.get(stage)
        if bucket is None:
            bucket = deque(maxlen=self._max_samples)
            self._samples[stage] = bucket
        bucket.append(float(ms))

    @staticmethod
    def _percentile(sorted_samples: list[float], pct: float) -> float:
        # Nearest-rank method — simple, deterministic, and matches what
        # most ops dashboards (Grafana, Datadog) use for low-volume series.
        if not sorted_samples:
            return 0.0
        if len(sorted_samples) == 1:
            return sorted_samples[0]
        k = max(0, min(len(sorted_samples) - 1, int(round(pct / 100.0 * (len(sorted_samples) - 1)))))
        return sorted_samples[k]

    def summary(self, stage: str) -> dict[str, float | int]:
        bucket = self._samples.get(stage)
        if not bucket:
            return {"count": 0, "p50": 0.0, "p95": 0.0, "p99": 0.0, "max": 0.0, "mean": 0.0}
        samples = sorted(bucket)
        count = len(samples)
        return {
            "count": count,
            "p50": self._percentile(samples, 50),
            "p95": self._percentile(samples, 95),
            "p99": self._percentile(samples, 99),
            "max": samples[-1],
            "mean": sum(samples) / count,
        }

    def summary_all(self) -> dict[str, dict[str, float | int]]:
        return {stage: self.summary(stage) for stage in list(self._samples.keys())}

    def reset(self) -> None:
        """Clear all samples — for tests."""
        self._samples.clear()

    @asynccontextmanager
    async def time_block(self, stage: str) -> AsyncIterator[None]:
        # perf_counter is the right clock for elapsed-time measurements
        # (monotonic, high resolution, immune to wall-clock adjustments).
        start = time.perf_counter()
        try:
            yield
        finally:
            elapsed_ms = (time.perf_counter() - start) * 1000.0
            await self.record(stage, elapsed_ms)


_tracker: LatencyTracker | None = None


def get_latency_tracker() -> LatencyTracker:
    global _tracker
    if _tracker is None:
        _tracker = LatencyTracker()
    return _tracker


def reset_latency_tracker() -> None:
    """Drop the singleton — for test isolation."""
    global _tracker
    _tracker = None
