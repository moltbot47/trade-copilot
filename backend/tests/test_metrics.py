"""Tests for /metrics Prometheus endpoint."""
from __future__ import annotations


def test_metrics_endpoint_returns_200(client):
    res = client.get("/metrics")
    assert res.status_code == 200


def test_metrics_endpoint_content_type(client):
    res = client.get("/metrics")
    # Prometheus content type is "text/plain; version=0.0.4; charset=utf-8"
    assert res.headers["content-type"].startswith("text/plain")


def test_metrics_contains_prometheus_format_markers(client):
    res = client.get("/metrics")
    body = res.text
    # Prometheus text format always has these markers if any metrics are registered.
    assert "# HELP" in body
    assert "# TYPE" in body


def test_metrics_includes_app_counters(client):
    """After hitting another endpoint, metrics text should reference our counters."""
    client.get("/health")
    res = client.get("/metrics")
    body = res.text
    # Check that at least one of our app-defined counters is present.
    assert (
        "tc_http_requests_total" in body
        or "tc_tradelocker_orders_total" in body
        or "tc_strategy_signals_total" in body
    )


def test_metrics_safe_to_call_repeatedly(client):
    """Calling metrics multiple times should not error out."""
    for _ in range(3):
        res = client.get("/metrics")
        assert res.status_code == 200
