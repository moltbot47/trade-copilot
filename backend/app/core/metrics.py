"""Prometheus-compatible metrics for Trade Copilot.

Exports counters, histograms, and gauges. Safe to import even if
prometheus_client isn't installed (uses no-op shims).
"""
from __future__ import annotations

try:  # pragma: no cover - prefer real prometheus_client
    from prometheus_client import (
        CONTENT_TYPE_LATEST,
        Counter,
        Gauge,
        Histogram,
        generate_latest,
    )
    PROMETHEUS_AVAILABLE = True
except Exception:  # pragma: no cover
    PROMETHEUS_AVAILABLE = False
    CONTENT_TYPE_LATEST = "text/plain; version=0.0.4; charset=utf-8"

    class _Noop:
        def labels(self, *_a, **_kw): return self
        def inc(self, *_a, **_kw): pass
        def dec(self, *_a, **_kw): pass
        def set(self, *_a, **_kw): pass
        def observe(self, *_a, **_kw): pass

    def Counter(*_a, **_kw): return _Noop()  # type: ignore[no-redef]
    def Gauge(*_a, **_kw): return _Noop()  # type: ignore[no-redef]
    def Histogram(*_a, **_kw): return _Noop()  # type: ignore[no-redef]

    def generate_latest(*_a, **_kw) -> bytes:  # type: ignore[misc]
        return b"# prometheus_client not installed\n"


# --- HTTP metrics ---
request_count = Counter(
    "tc_http_requests_total",
    "Total HTTP requests received.",
    ["method", "path", "status_code"],
)

request_duration_seconds = Histogram(
    "tc_http_request_duration_seconds",
    "HTTP request duration in seconds.",
    ["method", "path"],
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0),
)

# --- Trading metrics ---
tradelocker_orders_total = Counter(
    "tc_tradelocker_orders_total",
    "Total orders sent to TradeLocker.",
    ["side", "status"],
)

strategy_signals_total = Counter(
    "tc_strategy_signals_total",
    "Total strategy signals emitted.",
    ["bot", "action"],
)

# --- App metrics ---
active_users = Gauge(
    "tc_active_users",
    "Currently active users (sessions in last 5m).",
)


def render_prometheus() -> tuple[bytes, str]:
    """Return (body_bytes, content_type) for /metrics endpoint."""
    return generate_latest(), CONTENT_TYPE_LATEST
