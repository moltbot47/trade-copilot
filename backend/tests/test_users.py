"""User registration + identity tests."""
from __future__ import annotations


def test_create_user_success(client):
    res = client.post(
        "/api/users",
        json={"email": "alice@example.com", "password": "supersecret"},
    )
    assert res.status_code == 201, res.text
    body = res.json()
    assert body["email"] == "alice@example.com"
    assert "id" in body
    assert body["is_active"] is True


def test_create_user_duplicate_email(client):
    payload = {"email": "dup@example.com", "password": "supersecret"}
    r1 = client.post("/api/users", json=payload)
    assert r1.status_code == 201
    r2 = client.post("/api/users", json=payload)
    assert r2.status_code == 409
    assert "already" in r2.json()["detail"].lower()


def test_create_user_validates_email(client):
    res = client.post(
        "/api/users",
        json={"email": "not-an-email", "password": "supersecret"},
    )
    # Pydantic EmailStr → 422
    assert res.status_code == 422


def test_create_user_password_min_length(client):
    res = client.post(
        "/api/users",
        json={"email": "short@example.com", "password": "abc"},
    )
    assert res.status_code == 422


def test_get_me_without_auth_returns_401(client):
    res = client.get("/api/users/me")
    assert res.status_code == 401


def test_get_me_with_auth_creates_or_returns_user(client, auth_headers):
    res = client.get("/api/users/me", headers=auth_headers)
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["email"] == "tester@example.com"
    assert body["is_active"] is True


def test_get_me_is_idempotent(client, auth_headers):
    a = client.get("/api/users/me", headers=auth_headers).json()
    b = client.get("/api/users/me", headers=auth_headers).json()
    assert a["id"] == b["id"]
