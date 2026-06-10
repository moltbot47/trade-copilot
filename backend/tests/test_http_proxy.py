"""Tests for HttpProxyStrategy — the external-HTTP partner delivery path."""
import hashlib
import hmac
import json

import httpx
import pandas as pd
import pytest

from app.strategies.http_proxy import HttpProxyStrategy


def _bars(n: int = 20) -> pd.DataFrame:
    idx = pd.date_range("2026-06-10", periods=n, freq="1min", tz="UTC")
    return pd.DataFrame(
        {
            "open": [1.0 + i * 0.01 for i in range(n)],
            "high": [1.1 + i * 0.01 for i in range(n)],
            "low": [0.9 + i * 0.01 for i in range(n)],
            "close": [1.05 + i * 0.01 for i in range(n)],
            "volume": [10 + i for i in range(n)],
        },
        index=idx,
    )


def _strategy(handler, secret="s3cr3t"):
    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(transport=transport)
    return HttpProxyStrategy(
        slug="velocity_spike",
        endpoint_url="https://vlad.example.com/signal",
        secret=secret,
        timeframe="1m",
        client=client,
    )


@pytest.mark.asyncio
async def test_valid_signal_parsed():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "signal": {
                    "side": "buy",
                    "entry_price": 1.05,
                    "stop_loss": 1.00,
                    "take_profit": 1.20,
                    "qty": 0.02,
                    "early_stop_condition": "stall>3",
                }
            },
        )

    strat = _strategy(handler)
    sig = await strat.on_bar("NAS100", _bars())
    assert sig is not None
    assert sig.side == "buy"
    assert sig.symbol == "NAS100"  # forced to our symbol
    assert sig.entry_price == 1.05
    assert sig.qty == 0.02
    assert sig.early_stop_condition == "stall>3"
    # audit field defaulted from entry/stop
    assert sig.hard_stop_distance_pts == pytest.approx(0.05)


@pytest.mark.asyncio
async def test_top_level_signal_fields_accepted():
    def handler(request):
        return httpx.Response(
            200,
            json={"side": "sell", "entry_price": 2.0, "stop_loss": 2.1, "take_profit": 1.7},
        )

    sig = await _strategy(handler).on_bar("NAS100", _bars())
    assert sig is not None and sig.side == "sell"


@pytest.mark.asyncio
async def test_no_trade_responses_return_none():
    for body in ({"signal": None}, {}, {"signal": False}):
        def handler(request, _b=body):
            return httpx.Response(200, json=_b)

        assert await _strategy(handler).on_bar("NAS100", _bars()) is None


@pytest.mark.asyncio
async def test_non_2xx_returns_none():
    def handler(request):
        return httpx.Response(500, text="boom")

    assert await _strategy(handler).on_bar("NAS100", _bars()) is None


@pytest.mark.asyncio
async def test_malformed_json_returns_none():
    def handler(request):
        return httpx.Response(200, text="not json")

    assert await _strategy(handler).on_bar("NAS100", _bars()) is None


@pytest.mark.asyncio
async def test_missing_price_field_returns_none():
    def handler(request):
        return httpx.Response(200, json={"signal": {"side": "buy", "entry_price": 1.0}})

    assert await _strategy(handler).on_bar("NAS100", _bars()) is None


@pytest.mark.asyncio
async def test_bad_side_returns_none():
    def handler(request):
        return httpx.Response(
            200,
            json={"signal": {"side": "hold", "entry_price": 1, "stop_loss": 1, "take_profit": 1}},
        )

    assert await _strategy(handler).on_bar("NAS100", _bars()) is None


@pytest.mark.asyncio
async def test_network_error_returns_none():
    def handler(request):
        raise httpx.ConnectError("down", request=request)

    assert await _strategy(handler).on_bar("NAS100", _bars()) is None


@pytest.mark.asyncio
async def test_request_is_hmac_signed():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = request.content
        captured["sig"] = request.headers.get("X-Signature")
        captured["strat"] = request.headers.get("X-Strategy")
        return httpx.Response(200, json={"signal": None})

    await _strategy(handler, secret="topsecret").on_bar("NAS100", _bars())
    expected = "sha256=" + hmac.new(
        b"topsecret", captured["body"], hashlib.sha256
    ).hexdigest()
    assert captured["sig"] == expected
    assert captured["strat"] == "velocity_spike"
    # body carries our symbol + the bar window
    payload = json.loads(captured["body"])
    assert payload["symbol"] == "NAS100"
    assert len(payload["bars"]) == 20


@pytest.mark.asyncio
async def test_empty_bars_returns_none_without_call():
    called = {"n": 0}

    def handler(request):
        called["n"] += 1
        return httpx.Response(200, json={"signal": None})

    sig = await _strategy(handler).on_bar("NAS100", _bars(0))
    assert sig is None
    assert called["n"] == 0
