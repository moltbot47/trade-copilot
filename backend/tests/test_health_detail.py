"""Tests for /health/detail readiness endpoint."""
from __future__ import annotations

import httpx
import respx


def test_health_detail_returns_checks_dict_when_tradelocker_ok(client):
    """When TL responds OK and DB is healthy, overall status is 'ok'."""
    with respx.mock(assert_all_called=False) as r:
        r.get(url__regex=r".*tradelocker\.com/backend-api.*").respond(200, text="OK")
        res = client.get("/health/detail")
    assert res.status_code == 200
    body = res.json()
    assert "checks" in body
    assert "database" in body["checks"]
    assert "tradelocker_demo" in body["checks"]
    assert body["checks"]["database"]["status"] == "ok"
    assert body["status"] == "ok"
    assert "version" in body
    assert "uptime_seconds" in body


def test_health_detail_db_check_has_status_field(client):
    with respx.mock(assert_all_called=False) as r:
        r.get(url__regex=r".*tradelocker\.com/backend-api.*").respond(200, text="OK")
        res = client.get("/health/detail")
    body = res.json()
    assert body["checks"]["database"]["status"] in ("ok", "fail")
    assert "latency_ms" in body["checks"]["database"]


def test_health_detail_tradelocker_check_present(client):
    with respx.mock(assert_all_called=False) as r:
        r.get(url__regex=r".*tradelocker\.com/backend-api.*").respond(200, text="OK")
        res = client.get("/health/detail")
    body = res.json()
    assert "tradelocker_demo" in body["checks"]
    assert "status" in body["checks"]["tradelocker_demo"]


def test_health_detail_degraded_when_tradelocker_fails(client):
    """If TradeLocker times out or returns 5xx, overall is 'degraded' with HTTP 200."""
    with respx.mock(assert_all_called=False) as r:
        r.get(url__regex=r".*tradelocker\.com/backend-api.*").mock(
            side_effect=httpx.ConnectError("boom")
        )
        res = client.get("/health/detail")
    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "degraded"
    assert body["checks"]["tradelocker_demo"]["status"] == "fail"
    assert body["checks"]["database"]["status"] == "ok"


def test_health_detail_degraded_when_tradelocker_5xx(client):
    """5xx from TL = fail."""
    with respx.mock(assert_all_called=False) as r:
        r.get(url__regex=r".*tradelocker\.com/backend-api.*").respond(503, text="down")
        res = client.get("/health/detail")
    body = res.json()
    assert body["status"] == "degraded"
    assert body["checks"]["tradelocker_demo"]["status"] == "fail"


def test_health_detail_2xx_3xx_4xx_tradelocker_all_ok(client):
    """4xx is reachable (auth required) — counts as ok for connectivity."""
    with respx.mock(assert_all_called=False) as r:
        r.get(url__regex=r".*tradelocker\.com/backend-api.*").respond(401, text="auth")
        res = client.get("/health/detail")
    body = res.json()
    assert body["checks"]["tradelocker_demo"]["status"] == "ok"
