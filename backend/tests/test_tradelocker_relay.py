"""Tests for the TradeLocker → WS event-bus relay (Wave 3B).

Covers:
  * RelayManager start/stop semantics (idempotent, cancellable)
  * TradeLockerRelay diff/publish behaviour against a mocked client
  * Position diff helper (opened/closed/updated + debounce)
  * 401 → token refresh path
"""
from __future__ import annotations

import asyncio
import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.core.tradelocker_client import TradeLockerError
from app.ws.relay_manager import RelayManager
from app.ws.tradelocker_relay import (
    TradeLockerRelay,
    diff_positions,
    map_account_state,
    map_position,
)


# ---------- Helpers ----------


class _CapturingBus:
    """In-memory stand-in for app.ws.event_bus."""

    def __init__(self) -> None:
        self.events: list[tuple[str, int, dict]] = []
        self.global_events: list[tuple[str, dict]] = []

    def publish(self, channel: str, user_id: int, payload: dict) -> None:
        self.events.append((channel, user_id, dict(payload)))

    def publish_global(self, channel: str, payload: dict) -> None:
        self.global_events.append((channel, dict(payload)))


@pytest.fixture
def capturing_bus(monkeypatch):
    """Patch app.ws.event_bus with an in-memory capturing stand-in."""
    bus = _CapturingBus()

    # Build a fake module so `from app.ws.event_bus import event_bus` works.
    import sys
    import types

    mod = types.ModuleType("app.ws.event_bus")
    mod.event_bus = bus  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "app.ws.event_bus", mod)
    return bus


# ---------- map helpers ----------


def test_map_account_state_projects_documented_fields():
    raw = {
        "balance": 9999.78,
        "projectedBalance": 10000.55,
        "availableFunds": 9500.0,
        "openGrossPnL": 1.23,
        "todayNet": -0.22,
        "positionsCount": 3,
    }
    out = map_account_state(raw)
    assert out == {
        "balance": 9999.78,
        "equity": 10000.55,
        "available_funds": 9500.0,
        "open_pnl": 1.23,
        "today_net": -0.22,
        "positions_count": 3,
    }


def test_map_account_state_falls_back_to_balance_when_no_projected():
    raw = {"balance": 100.0, "positionsCount": 0}
    out = map_account_state(raw)
    assert out["equity"] == 100.0
    assert out["positions_count"] == 0


def test_map_position_normalises_types():
    raw = {
        "id": 7566047373987462950,
        "tradableInstrumentId": 278,
        "side": "buy",
        "qty": "0.01",
        "avgPrice": "80295.8",
        "unrealizedPl": "-0.13",
    }
    out = map_position(raw)
    assert out["id"] == "7566047373987462950"
    assert out["qty"] == 0.01
    assert out["avg_price"] == 80295.8
    assert out["unrealized_pl"] == -0.13
    assert out["side"] == "buy"


# ---------- diff_positions ----------


def test_diff_positions_opened_and_closed():
    prev: dict[str, dict] = {}
    curr = {"1": {"id": "1", "qty": 0.01, "unrealized_pl": 0.0}}
    last_emit: dict[str, float] = {}
    events = diff_positions(prev, curr, last_emit, now=100.0)
    assert len(events) == 1
    assert events[0]["kind"] == "opened"

    # Now closed
    events2 = diff_positions(curr, {}, last_emit, now=200.0)
    assert len(events2) == 1
    assert events2[0]["kind"] == "closed"


def test_diff_positions_updated_debounced():
    prev = {"1": {"id": "1", "qty": 0.01, "unrealized_pl": 0.0}}
    curr = {"1": {"id": "1", "qty": 0.01, "unrealized_pl": 1.5}}
    last_emit: dict[str, float] = {}

    # First update fires
    events = diff_positions(prev, curr, last_emit, now=100.0, debounce_s=5.0)
    assert len(events) == 1 and events[0]["kind"] == "updated"

    # Within debounce window — suppressed
    curr2 = {"1": {"id": "1", "qty": 0.01, "unrealized_pl": 2.0}}
    events2 = diff_positions(curr, curr2, last_emit, now=102.0, debounce_s=5.0)
    assert events2 == []

    # After debounce — fires again
    curr3 = {"1": {"id": "1", "qty": 0.01, "unrealized_pl": 3.0}}
    events3 = diff_positions(curr2, curr3, last_emit, now=110.0, debounce_s=5.0)
    assert len(events3) == 1 and events3[0]["kind"] == "updated"


def test_diff_positions_unchanged_emits_nothing():
    prev = {"1": {"id": "1", "qty": 0.01, "unrealized_pl": 0.0}}
    last_emit: dict[str, float] = {}
    events = diff_positions(prev, prev, last_emit, now=100.0)
    assert events == []


# ---------- RelayManager ----------


@pytest.mark.asyncio
async def test_relay_manager_start_for_user_spawns_task():
    started = asyncio.Event()

    async def fake_relay(uid: int) -> None:
        started.set()
        await asyncio.sleep(60)

    rm = RelayManager(factory=fake_relay)
    task = rm.start_for_user(42)
    assert task is not None
    await asyncio.wait_for(started.wait(), timeout=1.0)
    assert rm.is_running(42)
    await rm.stop_for_user(42)
    assert not rm.is_running(42)


@pytest.mark.asyncio
async def test_relay_manager_start_is_idempotent():
    async def fake_relay(uid: int) -> None:
        await asyncio.sleep(60)

    rm = RelayManager(factory=fake_relay)
    t1 = rm.start_for_user(7)
    t2 = rm.start_for_user(7)
    assert t1 is t2
    await rm.stop_for_user(7)


@pytest.mark.asyncio
async def test_relay_manager_stop_for_unknown_user_is_noop():
    rm = RelayManager(factory=lambda uid: asyncio.sleep(0))
    # Doesn't raise.
    await rm.stop_for_user(999)


@pytest.mark.asyncio
async def test_relay_manager_active_user_ids_tracks_running():
    async def fake_relay(uid: int) -> None:
        await asyncio.sleep(60)

    rm = RelayManager(factory=fake_relay)
    rm.start_for_user(1)
    rm.start_for_user(2)
    assert sorted(rm.active_user_ids()) == [1, 2]
    await rm.stop_all()
    assert rm.active_user_ids() == []


# ---------- TradeLockerRelay end-to-end ----------


def _build_relay(client, creds_loader, capturing_bus):
    relay = TradeLockerRelay(user_id=42, client=client)
    # Override the credential loader to skip DB.
    relay._load_creds = creds_loader  # type: ignore[assignment]
    return relay


@pytest.mark.asyncio
async def test_relay_publishes_account_event_on_first_run(capturing_bus):
    client = MagicMock()
    client.env = "demo"
    client.get_account_state = AsyncMock(
        return_value={
            "balance": 10_000.0,
            "projectedBalance": 10_010.0,
            "availableFunds": 9_500.0,
            "openGrossPnL": 1.0,
            "todayNet": 0.5,
            "positionsCount": 0,
        }
    )
    client.get_positions = AsyncMock(return_value=[])

    creds = {
        "token": "tok",
        "refresh": "ref",
        "account_id": "1",
        "acc_num": "1",
        "env": "demo",
    }
    relay = _build_relay(client, lambda: creds, capturing_bus)
    await relay.poll_once(client, creds)

    account_events = [e for e in capturing_bus.events if e[0] == "account"]
    assert len(account_events) == 1
    assert account_events[0][1] == 42
    assert account_events[0][2]["balance"] == 10_000.0


@pytest.mark.asyncio
async def test_relay_skips_publish_when_state_unchanged(capturing_bus):
    client = MagicMock()
    client.env = "demo"
    state = {
        "balance": 10_000.0,
        "projectedBalance": 10_000.0,
        "availableFunds": 10_000.0,
        "openGrossPnL": 0.0,
        "todayNet": 0.0,
        "positionsCount": 0,
    }
    client.get_account_state = AsyncMock(return_value=state)
    client.get_positions = AsyncMock(return_value=[])

    creds = {
        "token": "tok", "refresh": None, "account_id": "1",
        "acc_num": "1", "env": "demo",
    }
    relay = _build_relay(client, lambda: creds, capturing_bus)
    await relay.poll_once(client, creds)
    await relay.poll_once(client, creds)

    account_events = [e for e in capturing_bus.events if e[0] == "account"]
    assert len(account_events) == 1  # only the first one


@pytest.mark.asyncio
async def test_relay_republishes_when_balance_changes(capturing_bus):
    client = MagicMock()
    client.env = "demo"
    states = [
        {"balance": 10_000.0, "positionsCount": 0},
        {"balance": 9_999.0, "positionsCount": 0},
    ]
    client.get_account_state = AsyncMock(side_effect=states)
    client.get_positions = AsyncMock(return_value=[])

    creds = {
        "token": "tok", "refresh": None, "account_id": "1",
        "acc_num": "1", "env": "demo",
    }
    relay = _build_relay(client, lambda: creds, capturing_bus)
    await relay.poll_once(client, creds)
    await relay.poll_once(client, creds)

    account_events = [e for e in capturing_bus.events if e[0] == "account"]
    assert len(account_events) == 2
    assert account_events[1][2]["balance"] == 9_999.0


@pytest.mark.asyncio
async def test_relay_diffs_positions_opened_then_closed(capturing_bus):
    client = MagicMock()
    client.env = "demo"
    client.get_account_state = AsyncMock(
        return_value={"balance": 100.0, "positionsCount": 1}
    )
    pos_open = [
        {
            "id": "abc",
            "tradableInstrumentId": 278,
            "side": "buy",
            "qty": 0.01,
            "avgPrice": 80_000.0,
            "unrealizedPl": 0.0,
        }
    ]
    client.get_positions = AsyncMock(side_effect=[pos_open, pos_open, []])

    creds = {
        "token": "tok", "refresh": None, "account_id": "1",
        "acc_num": "1", "env": "demo",
    }
    relay = _build_relay(client, lambda: creds, capturing_bus)

    await relay.poll_once(client, creds)
    pos_events = [e for e in capturing_bus.events if e[0] == "positions"]
    assert any(e[2]["kind"] == "opened" for e in pos_events)

    await relay.poll_once(client, creds)
    # Same payload → no new positions event.
    pos_events_2 = [e for e in capturing_bus.events if e[0] == "positions"]
    assert len(pos_events_2) == 1

    await relay.poll_once(client, creds)
    pos_events_3 = [e for e in capturing_bus.events if e[0] == "positions"]
    assert any(e[2]["kind"] == "closed" for e in pos_events_3)


@pytest.mark.asyncio
async def test_relay_handles_401_by_refreshing_token(capturing_bus, monkeypatch):
    client = MagicMock()
    client.env = "demo"
    # First state call → 401, then second succeeds.
    client.get_account_state = AsyncMock(
        side_effect=TradeLockerError("unauthorized (401) - token expired")
    )
    client.get_positions = AsyncMock(return_value=[])
    client.refresh_access_token = AsyncMock(
        return_value={
            "access_token": "new-access",
            "refresh_token": "new-refresh",
            "expire_date": "2099-01-01T00:00:00Z",
        }
    )

    creds = {
        "token": "old-tok", "refresh": "old-ref",
        "account_id": "1", "acc_num": "1", "env": "demo",
    }
    relay = TradeLockerRelay(user_id=42, client=client, poll_interval_s=0.05)

    persisted: dict[str, Any] = {}

    def fake_load_creds() -> dict[str, Any]:
        # First call returns old token; after refresh the test verifies via persisted dict.
        return creds

    def fake_persist(access: str, refresh):
        persisted["access"] = access
        persisted["refresh"] = refresh

    relay._load_creds = fake_load_creds  # type: ignore[assignment]
    relay._persist_refreshed = fake_persist  # type: ignore[assignment]

    new_token = await relay._try_refresh(client, creds["refresh"])
    assert new_token == "new-access"
    assert persisted == {"access": "new-access", "refresh": "new-refresh"}


@pytest.mark.asyncio
async def test_relay_run_loop_stops_cleanly_on_cancel(capturing_bus):
    client = MagicMock()
    client.env = "demo"
    client.get_account_state = AsyncMock(
        return_value={"balance": 10.0, "positionsCount": 0}
    )
    client.get_positions = AsyncMock(return_value=[])

    creds = {
        "token": "tok", "refresh": None,
        "account_id": "1", "acc_num": "1", "env": "demo",
    }
    relay = TradeLockerRelay(user_id=42, client=client, poll_interval_s=0.05)
    relay._load_creds = lambda: creds  # type: ignore[assignment]

    task = asyncio.create_task(relay.run())
    await asyncio.sleep(0.15)  # let it tick a few times
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    account_events = [e for e in capturing_bus.events if e[0] == "account"]
    assert len(account_events) >= 1


@pytest.mark.asyncio
async def test_relay_pauses_when_no_creds(capturing_bus):
    """If user has no TL token, relay should idle without raising or publishing."""
    client = MagicMock()
    client.env = "demo"
    client.get_account_state = AsyncMock()
    client.get_positions = AsyncMock()

    relay = TradeLockerRelay(user_id=42, client=client, poll_interval_s=0.05)
    relay._load_creds = lambda: None  # type: ignore[assignment]

    task = asyncio.create_task(relay.run())
    await asyncio.sleep(0.1)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    # Never tried to fetch state.
    client.get_account_state.assert_not_called()
    assert capturing_bus.events == []


@pytest.mark.asyncio
async def test_relay_publishes_global_error_on_persistent_401(capturing_bus):
    client = MagicMock()
    client.env = "demo"
    client.get_account_state = AsyncMock(
        side_effect=TradeLockerError("unauthorized (401) - token rejected")
    )
    client.get_positions = AsyncMock(return_value=[])
    client.refresh_access_token = AsyncMock(
        side_effect=TradeLockerError("refresh failed")
    )

    creds = {
        "token": "bad", "refresh": "also-bad",
        "account_id": "1", "acc_num": "1", "env": "demo",
    }
    # Use a tiny backoff so the test doesn't actually sleep 5min.
    import app.ws.tradelocker_relay as tlr_mod

    orig_backoff = tlr_mod.AUTH_BACKOFF_S
    tlr_mod.AUTH_BACKOFF_S = 0.05
    try:
        relay = TradeLockerRelay(user_id=42, client=client, poll_interval_s=0.05)
        relay._load_creds = lambda: creds  # type: ignore[assignment]
        relay._persist_refreshed = lambda *a, **k: None  # type: ignore[assignment]

        task = asyncio.create_task(relay.run())
        await asyncio.sleep(0.2)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    finally:
        tlr_mod.AUTH_BACKOFF_S = orig_backoff

    err_events = [e for e in capturing_bus.global_events if e[0] == "error"]
    assert err_events, "expected at least one global error event for persistent 401"
    assert err_events[0][1]["scope"] == "tradelocker_relay"
    assert err_events[0][1]["user_id"] == 42
