"""Calculator endpoint tests — public, no auth."""
from __future__ import annotations


def test_preview_default_inputs_returns_expected_math(client):
    res = client.post(
        "/api/calculator/preview",
        json={"start_balance": 10_000, "daily_rate_pct": 1.0, "days": 100},
    )
    assert res.status_code == 200
    body = res.json()
    # 10_000 * 1.01^100 ≈ 27048.14
    assert abs(body["end_balance"] - 27048.14) < 1.0
    assert body["start_balance"] == 10_000
    assert body["days"] == 100
    assert body["daily_rate_pct"] == 1.0
    assert "milestones" in body
    assert isinstance(body["milestones"], dict)


def test_preview_2pct_100days(client):
    res = client.post(
        "/api/calculator/preview",
        json={"start_balance": 10_000, "daily_rate_pct": 2.0, "days": 100},
    )
    assert res.status_code == 200
    body = res.json()
    # 10_000 * 1.02^100 ≈ 72446.46
    assert abs(body["end_balance"] - 72446.46) < 1.0


def test_preview_clamps_min_inputs(client):
    # daily_rate_pct must be > 0 and ≤ 10
    res = client.post(
        "/api/calculator/preview",
        json={"start_balance": 100, "daily_rate_pct": 0.0, "days": 100},
    )
    assert res.status_code == 422


def test_preview_clamps_max_inputs(client):
    res = client.post(
        "/api/calculator/preview",
        json={"start_balance": 10_000, "daily_rate_pct": 50.0, "days": 100},
    )
    assert res.status_code == 422


def test_preview_min_days_enforced(client):
    res = client.post(
        "/api/calculator/preview",
        json={"start_balance": 10_000, "daily_rate_pct": 1.0, "days": 1},
    )
    assert res.status_code == 422


def test_preview_max_days_enforced(client):
    res = client.post(
        "/api/calculator/preview",
        json={"start_balance": 10_000, "daily_rate_pct": 1.0, "days": 9999},
    )
    assert res.status_code == 422


def test_generate_returns_pdf(client):
    res = client.post(
        "/api/calculator/generate",
        json={"start_balance": 10_000, "daily_rate_pct": 2.0, "days": 30},
    )
    assert res.status_code == 200
    assert res.headers["content-type"] == "application/pdf"
    body = res.content
    assert body[:4] == b"%PDF"
    assert len(body) > 1000  # non-trivial PDF


def test_generate_with_invalid_inputs_returns_4xx(client):
    res = client.post(
        "/api/calculator/generate",
        json={"start_balance": -100, "daily_rate_pct": 1.0, "days": 30},
    )
    assert 400 <= res.status_code < 500


def test_generate_with_requester_label(client):
    res = client.post(
        "/api/calculator/generate",
        json={
            "start_balance": 10_000,
            "daily_rate_pct": 2.0,
            "days": 30,
            "requester_label": "Test User",
        },
    )
    assert res.status_code == 200
    assert res.content[:4] == b"%PDF"
