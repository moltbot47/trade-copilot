"""Health endpoint smoke tests."""
from __future__ import annotations


def test_health_ok(client):
    res = client.get("/health")
    assert res.status_code == 200
    assert res.json() == {"status": "ok"}


def test_health_returns_json(client):
    res = client.get("/health")
    assert res.headers["content-type"].startswith("application/json")
