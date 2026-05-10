"""Unit tests for BarFetcher (app/strategies/data_feed.py).

Before these tests, data_feed.py was 13% covered (144 of 166 stmts missing).
The risk surface here is large: the broker-history endpoint shape was reverse-
engineered, the synthetic fallback is the safety-net the runner relies on,
and the 401-token-refresh path is the only thing that prevents a 24h silent
go-dark. Cover all of those.

Test categories:
  - timeframe → seconds helper (_tf_seconds)
  - LRU cache eviction (_cache_put)
  - Synthetic-fallback path (deterministic, marked synthetic=True)
  - Synthetic on resolve_symbol failure (non-auth)
  - 401 → token-refresh → retry → synthetic when still bad
  - INFO route resolution (and fallback to TRADE route on miss)
  - _parse_history all three known shapes (barDetails / d-list / bars)
  - History parse: numeric coercion, seconds-vs-ms normalization
  - History fetch happy-path (returns DataFrame, caches result)
  - History HTTP error returns None
  - History 4xx returns None
"""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import httpx
import numpy as np
import pandas as pd
import pytest
import respx
from httpx import Response

from app.core.tradelocker_client import TradeLockerClient, TradeLockerError
from app.strategies.data_feed import BarFetcher, _tf_seconds


_DEMO_BASE = "https://demo.tradelocker.com/backend-api"


# ---------------- _tf_seconds ----------------

def test_tf_seconds_minute_format():
    assert _tf_seconds("1m") == 60
    assert _tf_seconds("5m") == 300
    assert _tf_seconds("15m") == 900


def test_tf_seconds_hour_format():
    assert _tf_seconds("1h") == 3600
    assert _tf_seconds("4h") == 14400


def test_tf_seconds_unknown_returns_default():
    """Unknown suffix → 60s default rather than crash."""
    assert _tf_seconds("1d") == 60
    assert _tf_seconds("") == 60


# ---------------- _parse_history ----------------

def test_parse_history_barDetails_shape():
    """Shape A: {"d": {"barDetails": [[t,o,h,l,c,v], ...]}}"""
    payload = {
        "d": {
            "barDetails": [
                [1715301600000, 1.10, 1.11, 1.09, 1.105, 1000],
                [1715301660000, 1.105, 1.115, 1.10, 1.110, 1200],
            ]
        }
    }
    df = BarFetcher._parse_history(payload)
    assert df is not None
    assert len(df) == 2
    assert list(df.columns) == ["open", "high", "low", "close", "volume"]
    assert df["close"].iloc[0] == 1.105
    assert df["volume"].iloc[1] == 1200


def test_parse_history_d_as_list_shape():
    """Shape B: {"d": [[t,o,h,l,c,v], ...]}"""
    payload = {
        "d": [
            [1715301600000, 1.10, 1.11, 1.09, 1.105, 1000],
        ]
    }
    df = BarFetcher._parse_history(payload)
    assert df is not None and len(df) == 1
    assert df["open"].iloc[0] == 1.10


def test_parse_history_bars_list_of_dicts_shape():
    """Shape C: {"bars": [{t,o,h,l,c,v}, ...]}"""
    payload = {
        "bars": [
            {"t": 1715301600000, "o": 1.10, "h": 1.11, "l": 1.09, "c": 1.105, "v": 999},
            {"t": 1715301660000, "o": 1.105, "h": 1.115, "l": 1.10, "c": 1.110, "v": 1100},
        ]
    }
    df = BarFetcher._parse_history(payload)
    assert df is not None
    assert len(df) == 2
    assert df["close"].iloc[-1] == 1.110


def test_parse_history_normalizes_seconds_to_ms():
    """Numeric timestamps that look like 10-digit seconds → multiplied by 1000."""
    payload = {
        "d": [
            [1715301600, 1.10, 1.11, 1.09, 1.105, 1000],  # 10 digits → seconds
        ]
    }
    df = BarFetcher._parse_history(payload)
    assert df is not None
    # Index should be year ~2024 — if we had treated it as ms it would be 1970
    assert df.index[0].year > 2000


def test_parse_history_returns_none_on_empty():
    """No recognisable shape → None (caller treats as failure)."""
    assert BarFetcher._parse_history({}) is None
    assert BarFetcher._parse_history({"d": {}}) is None
    assert BarFetcher._parse_history({"d": {"barDetails": []}}) is None


def test_parse_history_returns_none_when_no_timestamp_column():
    """Dict shape missing time field → None rather than KeyError."""
    payload = {"bars": [{"o": 1.10, "c": 1.105}]}
    assert BarFetcher._parse_history(payload) is None


def test_parse_history_drops_rows_with_no_close():
    """Rows where close coerces to NaN must be filtered out."""
    payload = {
        "bars": [
            {"t": 1715301600000, "o": 1.10, "h": 1.11, "l": 1.09, "c": 1.105, "v": 999},
            {"t": 1715301660000, "o": 1.10, "h": 1.11, "l": 1.09, "c": "bad", "v": 1000},
        ]
    }
    df = BarFetcher._parse_history(payload)
    assert df is not None
    assert len(df) == 1  # bad row dropped


# ---------------- _synthetic ----------------

def test_synthetic_is_deterministic_for_same_symbol_tf():
    df1 = BarFetcher._synthetic("EURUSD", "1m", 100)
    df2 = BarFetcher._synthetic("EURUSD", "1m", 100)
    pd.testing.assert_frame_equal(df1, df2)


def test_synthetic_has_correct_shape_and_columns():
    df = BarFetcher._synthetic("EURUSD", "1m", 50)
    assert len(df) == 50
    assert list(df.columns) == ["open", "high", "low", "close", "volume"]
    # High >= low for all bars (synthetic invariant)
    assert (df["high"] >= df["low"]).all()


def test_synthetic_differs_across_symbols():
    """Different symbols → different seeds → different walks."""
    df_eu = BarFetcher._synthetic("EURUSD", "1m", 50)
    df_btc = BarFetcher._synthetic("BTCUSD", "1m", 50)
    assert not df_eu["close"].equals(df_btc["close"])


# ---------------- LRU cache ----------------

def test_cache_evicts_oldest_when_full():
    """When the cache exceeds maxsize, the LEAST-RECENTLY-USED key is dropped."""
    client = TradeLockerClient(env="demo", timeout=1.0)
    bf = BarFetcher(client, "ACC-1", "TOK", "1", cache_maxsize=2)
    df = BarFetcher._synthetic("A", "1m", 10)
    bf._cache_put(("A", "1m"), df)
    bf._cache_put(("B", "1m"), df)
    bf._cache_put(("C", "1m"), df)  # should evict ("A", "1m")
    assert ("A", "1m") not in bf._cache
    assert ("B", "1m") in bf._cache
    assert ("C", "1m") in bf._cache


def test_cache_reinsert_moves_to_mru():
    """Re-putting an existing key bumps it to MRU position so a later put
    evicts a DIFFERENT (older) key."""
    client = TradeLockerClient(env="demo", timeout=1.0)
    bf = BarFetcher(client, "ACC-1", "TOK", "1", cache_maxsize=2)
    df = BarFetcher._synthetic("A", "1m", 10)
    bf._cache_put(("A", "1m"), df)
    bf._cache_put(("B", "1m"), df)
    # Touch A so it becomes MRU
    bf._cache_put(("A", "1m"), df)
    bf._cache_put(("C", "1m"), df)  # should evict B, not A
    assert ("B", "1m") not in bf._cache
    assert ("A", "1m") in bf._cache


def test_cache_maxsize_floor_is_one():
    """A 0 or negative maxsize gets clamped to 1 so we never have a zero-sized cache."""
    client = TradeLockerClient(env="demo", timeout=1.0)
    bf = BarFetcher(client, "ACC-1", "TOK", "1", cache_maxsize=0)
    assert bf._cache_maxsize == 1


# ---------------- _try_refresh_token ----------------

@pytest.mark.asyncio
async def test_try_refresh_token_returns_false_when_no_callback():
    client = TradeLockerClient(env="demo", timeout=1.0)
    bf = BarFetcher(client, "A", "OLD", "1", token_refresh_cb=None)
    assert await bf._try_refresh_token() is False
    assert bf.token == "OLD"


@pytest.mark.asyncio
async def test_try_refresh_token_rotates_token_on_success():
    client = TradeLockerClient(env="demo", timeout=1.0)

    async def cb():
        return "NEW_TOKEN"

    bf = BarFetcher(client, "A", "OLD", "1", token_refresh_cb=cb)
    assert await bf._try_refresh_token() is True
    assert bf.token == "NEW_TOKEN"


@pytest.mark.asyncio
async def test_try_refresh_token_returns_false_when_callback_returns_same():
    client = TradeLockerClient(env="demo", timeout=1.0)

    async def cb():
        return "OLD"

    bf = BarFetcher(client, "A", "OLD", "1", token_refresh_cb=cb)
    assert await bf._try_refresh_token() is False


@pytest.mark.asyncio
async def test_try_refresh_token_returns_false_when_callback_returns_none():
    client = TradeLockerClient(env="demo", timeout=1.0)

    async def cb():
        return None

    bf = BarFetcher(client, "A", "OLD", "1", token_refresh_cb=cb)
    assert await bf._try_refresh_token() is False
    assert bf.token == "OLD"


@pytest.mark.asyncio
async def test_try_refresh_token_swallows_callback_exception():
    """A broken callback must NOT propagate — we just stay with the old token."""
    client = TradeLockerClient(env="demo", timeout=1.0)

    async def cb():
        raise RuntimeError("oh no")

    bf = BarFetcher(client, "A", "OLD", "1", token_refresh_cb=cb)
    assert await bf._try_refresh_token() is False
    assert bf.token == "OLD"


# ---------------- _resolve_info_route ----------------

@pytest.mark.asyncio
async def test_resolve_info_route_returns_info_id():
    client = TradeLockerClient(env="demo", timeout=2.0)
    instruments_payload = {
        "d": {
            "instruments": [
                {
                    "name": "BTCUSD",
                    "tradableInstrumentId": 7,
                    "routes": [
                        {"id": 100, "type": "TRADE"},
                        {"id": 200, "type": "INFO"},
                    ],
                }
            ]
        }
    }
    with respx.mock(base_url=_DEMO_BASE) as r:
        r.get("/trade/accounts/A/instruments").mock(
            return_value=Response(200, json=instruments_payload)
        )
        bf = BarFetcher(client, "A", "TOK", "1")
        rid = await bf._resolve_info_route("BTCUSD")
    assert rid == 200


@pytest.mark.asyncio
async def test_resolve_info_route_caches_lookup():
    """A second call must NOT re-hit /instruments."""
    client = TradeLockerClient(env="demo", timeout=2.0)
    instruments_payload = {
        "d": {
            "instruments": [
                {
                    "name": "BTCUSD",
                    "tradableInstrumentId": 7,
                    "routes": [{"id": 200, "type": "INFO"}],
                }
            ]
        }
    }
    with respx.mock(base_url=_DEMO_BASE) as r:
        route = r.get("/trade/accounts/A/instruments").mock(
            return_value=Response(200, json=instruments_payload)
        )
        bf = BarFetcher(client, "A", "TOK", "1")
        await bf._resolve_info_route("BTCUSD")
        await bf._resolve_info_route("BTCUSD")
    assert route.call_count == 1


@pytest.mark.asyncio
async def test_resolve_info_route_returns_none_when_no_info_route():
    """Instrument exists but has no INFO-type route → None."""
    client = TradeLockerClient(env="demo", timeout=2.0)
    instruments_payload = {
        "d": {
            "instruments": [
                {
                    "name": "BTCUSD",
                    "tradableInstrumentId": 7,
                    "routes": [{"id": 100, "type": "TRADE"}],  # no INFO
                }
            ]
        }
    }
    with respx.mock(base_url=_DEMO_BASE) as r:
        r.get("/trade/accounts/A/instruments").mock(
            return_value=Response(200, json=instruments_payload)
        )
        bf = BarFetcher(client, "A", "TOK", "1")
        assert await bf._resolve_info_route("BTCUSD") is None


@pytest.mark.asyncio
async def test_resolve_info_route_returns_none_on_instruments_error():
    """If get_instruments raises (broker down) → None, no crash."""
    client = TradeLockerClient(env="demo", timeout=2.0)
    with patch(
        "app.core.tradelocker_client.TradeLockerClient.get_instruments",
        new=AsyncMock(side_effect=TradeLockerError("instruments down")),
    ):
        bf = BarFetcher(client, "A", "TOK", "1")
        assert await bf._resolve_info_route("BTCUSD") is None


# ---------------- fetch_bars ----------------

@pytest.mark.asyncio
async def test_fetch_bars_falls_back_to_synthetic_on_resolve_error():
    """Non-401 resolve_symbol failure → synthetic data, attrs marked."""
    client = TradeLockerClient(env="demo", timeout=2.0)
    with patch(
        "app.core.tradelocker_client.TradeLockerClient.resolve_symbol",
        new=AsyncMock(side_effect=TradeLockerError("symbol not found")),
    ):
        bf = BarFetcher(client, "A", "TOK", "1")
        df = await bf.fetch_bars("ZZZ", "1m", count=30)
    assert len(df) == 30
    assert df.attrs.get("synthetic") is True


@pytest.mark.asyncio
async def test_fetch_bars_falls_back_to_synthetic_on_401_no_refresh():
    """401 with NO refresh callback → synthetic (no retry)."""
    client = TradeLockerClient(env="demo", timeout=2.0)
    with patch(
        "app.core.tradelocker_client.TradeLockerClient.resolve_symbol",
        new=AsyncMock(side_effect=TradeLockerError("401 unauthorized")),
    ):
        bf = BarFetcher(client, "A", "OLD", "1", token_refresh_cb=None)
        df = await bf.fetch_bars("EURUSD", "1m", count=30)
    assert df.attrs.get("synthetic") is True


@pytest.mark.asyncio
async def test_fetch_bars_401_triggers_token_refresh_and_retry():
    """401 → refresh → resolve_symbol called again. On second failure → synthetic."""
    client = TradeLockerClient(env="demo", timeout=2.0)
    refreshed_to = "NEW_TOKEN"

    async def cb():
        return refreshed_to

    call_count = {"n": 0}

    async def fake_resolve(*args, **kwargs):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise TradeLockerError("401 unauthorized")
        # Even after refresh, still fails (resolve issue, not auth)
        raise TradeLockerError("still failing")

    bf = BarFetcher(client, "A", "OLD", "1", token_refresh_cb=cb)
    with patch(
        "app.core.tradelocker_client.TradeLockerClient.resolve_symbol",
        new=fake_resolve,
    ):
        df = await bf.fetch_bars("EURUSD", "1m", count=20)

    assert call_count["n"] == 2  # initial + retry after refresh
    assert bf.token == refreshed_to  # token rotated
    assert df.attrs.get("synthetic") is True


@pytest.mark.asyncio
async def test_fetch_bars_happy_path_uses_info_route_and_caches():
    """End-to-end: resolve → info-route lookup → history → cached, real bars."""
    client = TradeLockerClient(env="demo", timeout=2.0)
    instruments_payload = {
        "d": {
            "instruments": [
                {
                    "name": "EURUSD",
                    "tradableInstrumentId": 42,
                    "routes": [
                        {"id": 7, "type": "TRADE"},
                        {"id": 8, "type": "INFO"},
                    ],
                }
            ]
        }
    }
    # 5 recent bars
    bars = []
    base_t = 1715301600000
    for i in range(5):
        bars.append([base_t + i * 60_000, 1.10 + i * 0.001, 1.11, 1.09, 1.10 + i * 0.001, 1000])
    history_payload = {"d": {"barDetails": bars}}

    with respx.mock(base_url=_DEMO_BASE) as r:
        r.get("/trade/accounts/A/instruments").mock(
            return_value=Response(200, json=instruments_payload)
        )
        r.get("/trade/history").mock(return_value=Response(200, json=history_payload))

        bf = BarFetcher(client, "A", "TOK", "1")
        df = await bf.fetch_bars("EURUSD", "1m", count=5)

    assert not df.attrs.get("synthetic", False)
    assert len(df) == 5
    assert list(df.columns) == ["open", "high", "low", "close", "volume"]
    # Cache populated
    assert ("EURUSD", "1m") in bf._cache


@pytest.mark.asyncio
async def test_fetch_bars_returns_synthetic_when_all_resolutions_fail():
    """All _TF_CANDIDATES exhausted (history endpoint always 4xx) → synthetic."""
    client = TradeLockerClient(env="demo", timeout=2.0)
    instruments_payload = {
        "d": {
            "instruments": [
                {
                    "name": "EURUSD",
                    "tradableInstrumentId": 42,
                    "routes": [
                        {"id": 7, "type": "TRADE"},
                        {"id": 8, "type": "INFO"},
                    ],
                }
            ]
        }
    }
    with respx.mock(base_url=_DEMO_BASE) as r:
        r.get("/trade/accounts/A/instruments").mock(
            return_value=Response(200, json=instruments_payload)
        )
        # Every history attempt returns 400
        r.get("/trade/history").mock(return_value=Response(400, text="bad request"))
        bf = BarFetcher(client, "A", "TOK", "1")
        df = await bf.fetch_bars("EURUSD", "1m", count=20)

    assert df.attrs.get("synthetic") is True


@pytest.mark.asyncio
async def test_fetch_bars_falls_back_when_history_returns_non_json():
    """Broker returns 200 with non-JSON body → parse fails → synthetic."""
    client = TradeLockerClient(env="demo", timeout=2.0)
    instruments_payload = {
        "d": {
            "instruments": [
                {
                    "name": "EURUSD",
                    "tradableInstrumentId": 42,
                    "routes": [
                        {"id": 7, "type": "TRADE"},
                        {"id": 8, "type": "INFO"},
                    ],
                }
            ]
        }
    }
    with respx.mock(base_url=_DEMO_BASE, assert_all_called=False) as r:
        r.get("/trade/accounts/A/instruments").mock(
            return_value=Response(200, json=instruments_payload)
        )
        r.get("/trade/history").mock(return_value=Response(200, text="<html>nope</html>"))
        bf = BarFetcher(client, "A", "TOK", "1")
        df = await bf.fetch_bars("EURUSD", "1m", count=10)
    assert df.attrs.get("synthetic") is True


@pytest.mark.asyncio
async def test_fetch_bars_uses_trade_route_when_no_info_route():
    """If INFO route resolution returns None, we fall back to TRADE route."""
    client = TradeLockerClient(env="demo", timeout=2.0)
    instruments_payload = {
        "d": {
            "instruments": [
                {
                    "name": "EURUSD",
                    "tradableInstrumentId": 42,
                    "routes": [{"id": 7, "type": "TRADE"}],  # no INFO route
                }
            ]
        }
    }
    # Capture the routeId actually sent on /trade/history
    captured = {}

    def _capture(request):
        captured["routeId"] = request.url.params.get("routeId")
        return Response(400)  # let it fail to skip into synthetic

    with respx.mock(base_url=_DEMO_BASE) as r:
        r.get("/trade/accounts/A/instruments").mock(
            return_value=Response(200, json=instruments_payload)
        )
        r.get("/trade/history").mock(side_effect=_capture)
        bf = BarFetcher(client, "A", "TOK", "1")
        await bf.fetch_bars("EURUSD", "1m", count=10)

    # The trade route (id=7) was used because there was no INFO route
    assert captured["routeId"] == "7"


@pytest.mark.asyncio
async def test_fetch_bars_handles_httpx_transport_error():
    """If httpx itself raises (e.g. timeout / connect-error) during /trade/history,
    _try_history returns None and the next variant is tried (eventually synthetic).
    """
    client = TradeLockerClient(env="demo", timeout=2.0)

    with patch(
        "app.core.tradelocker_client.TradeLockerClient.resolve_symbol",
        new=AsyncMock(return_value=(42, 7)),
    ), patch(
        "app.core.tradelocker_client.TradeLockerClient.get_instruments",
        new=AsyncMock(return_value=[]),
    ), patch(
        "httpx.AsyncClient.get",
        new=AsyncMock(side_effect=httpx.ConnectError("connection refused")),
    ):
        bf = BarFetcher(client, "A", "TOK", "1")
        df = await bf.fetch_bars("EURUSD", "1m", count=10)
    assert df.attrs.get("synthetic") is True


@pytest.mark.asyncio
async def test_fetch_bars_merges_new_bars_with_cache_and_keeps_count():
    """A second fetch merges new bars with previously-cached bars,
    deduplicates by timestamp, and trims to `count`."""
    client = TradeLockerClient(env="demo", timeout=2.0)
    instruments_payload = {
        "d": {
            "instruments": [
                {
                    "name": "EURUSD",
                    "tradableInstrumentId": 42,
                    "routes": [
                        {"id": 7, "type": "TRADE"},
                        {"id": 8, "type": "INFO"},
                    ],
                }
            ]
        }
    }
    base_t = 1715301600000
    first_bars = [[base_t + i * 60_000, 1.0, 1.01, 0.99, 1.0, 100] for i in range(5)]
    # Second fetch has 3 new bars + 2 overlapping
    second_bars = [
        [base_t + i * 60_000, 2.0, 2.01, 1.99, 2.0, 200] for i in range(3, 8)
    ]
    payloads = [
        {"d": {"barDetails": first_bars}},
        {"d": {"barDetails": second_bars}},
    ]
    call_idx = {"n": 0}

    def _serve_history(request):
        idx = min(call_idx["n"], len(payloads) - 1)
        call_idx["n"] += 1
        return Response(200, json=payloads[idx])

    with respx.mock(base_url=_DEMO_BASE) as r:
        r.get("/trade/accounts/A/instruments").mock(
            return_value=Response(200, json=instruments_payload)
        )
        r.get("/trade/history").mock(side_effect=_serve_history)
        bf = BarFetcher(client, "A", "TOK", "1")
        df1 = await bf.fetch_bars("EURUSD", "1m", count=8)
        df2 = await bf.fetch_bars("EURUSD", "1m", count=8)

    assert len(df1) == 5
    # Second call merges → 8 unique bars total
    assert len(df2) == 8
    # Overlapping bars (idx 3, 4) keep LAST (the second-call values)
    overlap_close = df2.iloc[3]["close"]
    assert overlap_close == 2.0  # came from second payload (keep="last")
