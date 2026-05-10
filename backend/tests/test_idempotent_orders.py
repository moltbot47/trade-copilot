"""Tests for idempotent place_order — same client_order_id within the
60s window returns a cached response without re-hitting the broker.

The dedup cache is class-level so retry storms across multiple
TradeLockerClient instances (e.g. token rotation creates a new client)
still share dedup state.
"""
from __future__ import annotations

import time

import pytest

from app.core.tradelocker_client import TradeLockerClient


@pytest.fixture(autouse=True)
def clear_idempotency_cache():
    """Each test starts with a clean class-level cache."""
    TradeLockerClient._idempotency_cache.clear()
    yield
    TradeLockerClient._idempotency_cache.clear()


def test_idempotency_cache_hit_returns_true_within_window():
    client = TradeLockerClient(env="demo")
    client._idempotency_cache_put("test-key", {"order_id": "OID-1", "raw": {}})
    assert client._idempotency_cache_hit("test-key") is True


def test_idempotency_cache_miss_for_unknown_key():
    client = TradeLockerClient(env="demo")
    assert client._idempotency_cache_hit("never-seen") is False


def test_idempotency_cache_expires_after_ttl(monkeypatch):
    client = TradeLockerClient(env="demo")
    client._IDEMPOTENCY_TTL_SECONDS = 0.1  # short for test
    client._idempotency_cache_put("test-key", {"order_id": "OID-1", "raw": {}})
    assert client._idempotency_cache_hit("test-key") is True
    time.sleep(0.15)
    assert client._idempotency_cache_hit("test-key") is False


def test_idempotency_cache_is_shared_across_instances():
    """Token rotation creates a fresh client — dedup should still work."""
    c1 = TradeLockerClient(env="demo")
    c1._idempotency_cache_put("shared-key", {"order_id": "OID-X", "raw": {}})

    c2 = TradeLockerClient(env="demo")
    assert c2._idempotency_cache_hit("shared-key") is True


def test_idempotency_cache_evicts_old_when_full():
    """Cache should not grow unbounded."""
    client = TradeLockerClient(env="demo")
    # Pre-fill with old entries
    import time as _time
    for i in range(300):
        client._idempotency_cache[f"old-{i}"] = {
            "ts": _time.monotonic() - 120,  # past TTL
            "response": {"order_id": f"OID-{i}", "raw": {}},
        }
    # Adding a new entry triggers eviction
    client._idempotency_cache_put("new", {"order_id": "OID-NEW", "raw": {}})
    # All old entries should be gone, new one stays
    assert client._idempotency_cache_hit("new") is True
    assert all(
        not k.startswith("old-")
        for k in client._idempotency_cache.keys()
    )


@pytest.mark.asyncio
async def test_place_order_includes_client_order_id_in_body(monkeypatch):
    """When client_order_id is provided, it's added to the request body."""
    client = TradeLockerClient(env="demo")

    captured = {}

    async def fake_resolve(account_id, token, acc_num, symbol):
        return (123, 456)

    async def fake_request(method, path, *, token=None, acc_num=None, json=None, extra_headers=None):
        captured["method"] = method
        captured["path"] = path
        captured["json"] = json
        captured["extra_headers"] = extra_headers
        return {"s": "ok", "d": {"orderId": "BROKER-OID-1"}}

    monkeypatch.setattr(client, "resolve_symbol", fake_resolve)
    monkeypatch.setattr(client, "_request", fake_request)

    result = await client.place_order(
        account_id="1",
        token="t",
        acc_num="1",
        symbol="BTCUSD",
        side="buy",
        qty=0.01,
        sl=80000,
        tp=82000,
        client_order_id="tc-test-key",
    )

    assert captured["json"]["clientOrderId"] == "tc-test-key"
    assert captured["extra_headers"] == {"X-Idempotency-Key": "tc-test-key"}
    assert result["order_id"] == "BROKER-OID-1"
    assert result["idempotency_key"] == "tc-test-key"
    assert result["duplicate"] is False


@pytest.mark.asyncio
async def test_place_order_dedups_on_repeated_client_order_id(monkeypatch):
    """Calling place_order twice with the same client_order_id within TTL
    should hit the cache the second time, not the broker."""
    client = TradeLockerClient(env="demo")

    call_count = {"n": 0}

    async def fake_resolve(account_id, token, acc_num, symbol):
        return (123, 456)

    async def fake_request(method, path, *, token=None, acc_num=None, json=None, extra_headers=None):
        call_count["n"] += 1
        return {"s": "ok", "d": {"orderId": f"OID-{call_count['n']}"}}

    monkeypatch.setattr(client, "resolve_symbol", fake_resolve)
    monkeypatch.setattr(client, "_request", fake_request)

    r1 = await client.place_order(
        account_id="1", token="t", acc_num="1", symbol="BTCUSD",
        side="buy", qty=0.01, client_order_id="dedup-key",
    )
    r2 = await client.place_order(
        account_id="1", token="t", acc_num="1", symbol="BTCUSD",
        side="buy", qty=0.01, client_order_id="dedup-key",
    )

    assert call_count["n"] == 1  # broker only hit ONCE
    assert r1["order_id"] == r2["order_id"]  # both return same response
    assert r1["duplicate"] is False
    assert r2["duplicate"] is True  # second call flagged as dedup


@pytest.mark.asyncio
async def test_place_order_without_coid_does_not_dedup(monkeypatch):
    """Calls WITHOUT client_order_id should always hit the broker."""
    client = TradeLockerClient(env="demo")

    call_count = {"n": 0}

    async def fake_resolve(account_id, token, acc_num, symbol):
        return (123, 456)

    async def fake_request(method, path, *, token=None, acc_num=None, json=None, extra_headers=None):
        call_count["n"] += 1
        return {"s": "ok", "d": {"orderId": f"OID-{call_count['n']}"}}

    monkeypatch.setattr(client, "resolve_symbol", fake_resolve)
    monkeypatch.setattr(client, "_request", fake_request)

    r1 = await client.place_order(
        account_id="1", token="t", acc_num="1", symbol="BTCUSD",
        side="buy", qty=0.01,
    )
    r2 = await client.place_order(
        account_id="1", token="t", acc_num="1", symbol="BTCUSD",
        side="buy", qty=0.01,
    )

    assert call_count["n"] == 2  # broker hit twice
    assert r1["order_id"] != r2["order_id"]
    assert r1["duplicate"] is False
    assert r2["duplicate"] is False
