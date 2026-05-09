"""Subscription CRUD tests."""
from __future__ import annotations


def test_create_subscription(client, auth_headers, seed_bots):
    bot_id = seed_bots[0].id
    res = client.post(
        "/api/subscriptions",
        headers=auth_headers,
        json={"bot_id": bot_id, "aggression_level": 5},
    )
    assert res.status_code == 201, res.text
    body = res.json()
    assert body["bot_id"] == bot_id
    assert body["aggression_level"] == 5
    assert body["is_paused"] is False


def test_create_subscription_duplicate_returns_409(client, auth_headers, seed_bots):
    bot_id = seed_bots[0].id
    payload = {"bot_id": bot_id, "aggression_level": 5}
    r1 = client.post("/api/subscriptions", headers=auth_headers, json=payload)
    assert r1.status_code == 201
    r2 = client.post("/api/subscriptions", headers=auth_headers, json=payload)
    assert r2.status_code == 409


def test_create_subscription_unknown_bot_returns_404(client, auth_headers):
    res = client.post(
        "/api/subscriptions",
        headers=auth_headers,
        json={"bot_id": 9999, "aggression_level": 5},
    )
    assert res.status_code == 404


def test_list_my_subscriptions(client, auth_headers, seed_bots):
    bot_id = seed_bots[0].id
    client.post(
        "/api/subscriptions",
        headers=auth_headers,
        json={"bot_id": bot_id, "aggression_level": 7},
    )
    res = client.get("/api/subscriptions", headers=auth_headers)
    assert res.status_code == 200
    subs = res.json()
    assert len(subs) == 1
    assert subs[0]["bot_id"] == bot_id


def test_update_subscription_aggression(client, auth_headers, seed_bots):
    bot_id = seed_bots[0].id
    create = client.post(
        "/api/subscriptions",
        headers=auth_headers,
        json={"bot_id": bot_id, "aggression_level": 5},
    )
    sub_id = create.json()["id"]
    res = client.patch(
        f"/api/subscriptions/{sub_id}",
        headers=auth_headers,
        json={"aggression_level": 9},
    )
    assert res.status_code == 200
    assert res.json()["aggression_level"] == 9


def test_update_subscription_pause(client, auth_headers, seed_bots):
    bot_id = seed_bots[0].id
    sub_id = client.post(
        "/api/subscriptions",
        headers=auth_headers,
        json={"bot_id": bot_id, "aggression_level": 5},
    ).json()["id"]
    res = client.patch(
        f"/api/subscriptions/{sub_id}",
        headers=auth_headers,
        json={"is_paused": True},
    )
    assert res.status_code == 200
    assert res.json()["is_paused"] is True


def test_delete_subscription(client, auth_headers, seed_bots):
    bot_id = seed_bots[0].id
    sub_id = client.post(
        "/api/subscriptions",
        headers=auth_headers,
        json={"bot_id": bot_id, "aggression_level": 5},
    ).json()["id"]
    res = client.delete(f"/api/subscriptions/{sub_id}", headers=auth_headers)
    assert res.status_code == 200
    assert res.json()["status"] == "deleted"

    # Subsequent list call should be empty
    listed = client.get("/api/subscriptions", headers=auth_headers).json()
    assert listed == []


def test_aggression_out_of_range_rejected(client, auth_headers, seed_bots):
    bot_id = seed_bots[0].id
    res = client.post(
        "/api/subscriptions",
        headers=auth_headers,
        json={"bot_id": bot_id, "aggression_level": 99},
    )
    assert res.status_code == 422
