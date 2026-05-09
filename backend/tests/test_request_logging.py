"""Tests for RequestLoggingMiddleware — X-Request-ID, skip paths, error logging."""
from __future__ import annotations

import logging


def test_request_id_header_added_on_normal_response(client):
    res = client.get("/api/bots")
    assert "X-Request-ID" in res.headers
    assert len(res.headers["X-Request-ID"]) > 0


def test_request_id_passes_through_when_provided(client):
    res = client.get("/api/bots", headers={"X-Request-ID": "my-trace-id-123"})
    assert res.headers["X-Request-ID"] == "my-trace-id-123"


def test_health_excluded_from_app_request_logger(client, caplog):
    """The middleware skips /health and /metrics — no 'request' INFO log emitted."""
    caplog.set_level(logging.INFO, logger="app.request")
    caplog.clear()
    client.get("/health")
    health_records = [r for r in caplog.records if r.name == "app.request"]
    assert health_records == []


def test_metrics_excluded_from_app_request_logger(client, caplog):
    caplog.set_level(logging.INFO, logger="app.request")
    caplog.clear()
    client.get("/metrics")
    metrics_records = [r for r in caplog.records if r.name == "app.request"]
    assert metrics_records == []


def test_normal_request_is_logged(client, caplog):
    caplog.set_level(logging.INFO, logger="app.request")
    caplog.clear()
    res = client.get("/api/bots")
    assert res.status_code == 200
    rec = [r for r in caplog.records if r.name == "app.request"]
    assert len(rec) >= 1
    # Each app.request record carries method/path/status as extras.
    record = rec[0]
    assert record.method == "GET"
    assert record.path == "/api/bots"
    assert record.status == 200


def test_unknown_path_still_returns_request_id(client):
    res = client.get("/this/path/does/not/exist")
    assert res.status_code == 404
    assert "X-Request-ID" in res.headers
