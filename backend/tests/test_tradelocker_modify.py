"""Tests for TradeLockerClient.modify_position + partial_close (Wave 4B)."""
from __future__ import annotations

import pytest
import respx
from httpx import Response

from app.core.tradelocker_client import TradeLockerClient, TradeLockerError


_DEMO_BASE = "https://demo.tradelocker.com/backend-api"


@pytest.fixture()
def client() -> TradeLockerClient:
    return TradeLockerClient(env="demo", timeout=2.0)


@pytest.mark.asyncio
async def test_modify_position_uses_patch(client):
    """Verified endpoint: PATCH /trade/positions/{id} with {stopLoss, takeProfit}."""
    captured = {}

    def _capture(request):
        captured["body"] = request.read()
        return Response(200, json={"s": "ok"})

    with respx.mock(base_url=_DEMO_BASE) as r:
        route = r.patch("/trade/positions/POS-1").mock(side_effect=_capture)
        result = await client.modify_position(
            "POS-1", token="TOK", acc_num="4", stop_loss=100.0, take_profit=200.0
        )
    assert route.called
    body = captured["body"].decode()
    assert "stopLoss" in body and "100" in body
    assert "takeProfit" in body and "200" in body
    assert result.get("s") == "ok"


@pytest.mark.asyncio
async def test_modify_position_only_stop_loss(client):
    captured = {}

    def _capture(request):
        captured["body"] = request.read()
        return Response(200, json={"s": "ok"})

    with respx.mock(base_url=_DEMO_BASE) as r:
        r.patch("/trade/positions/POS-2").mock(side_effect=_capture)
        await client.modify_position("POS-2", token="T", acc_num="4", stop_loss=99.0)
    body = captured["body"].decode()
    assert "stopLoss" in body
    assert "takeProfit" not in body


@pytest.mark.asyncio
async def test_modify_position_requires_at_least_one_field(client):
    with pytest.raises(TradeLockerError):
        await client.modify_position("POS-3", token="T", acc_num="4")


@pytest.mark.asyncio
async def test_modify_position_handles_4xx(client):
    with respx.mock(base_url=_DEMO_BASE) as r:
        r.patch("/trade/positions/POS-4").mock(
            return_value=Response(404, json={"error": "not found"})
        )
        with pytest.raises(TradeLockerError):
            await client.modify_position("POS-4", token="T", acc_num="4", stop_loss=1.0)


@pytest.mark.asyncio
async def test_modify_position_rejects_when_response_indicates_error(client):
    """TL returns 200 with {s:'error', errmsg:...} for some failures."""
    with respx.mock(base_url=_DEMO_BASE) as r:
        r.patch("/trade/positions/POS-5").mock(
            return_value=Response(
                200, json={"s": "error", "errmsg": "Position not found"}
            )
        )
        with pytest.raises(TradeLockerError):
            await client.modify_position("POS-5", token="T", acc_num="4", stop_loss=1.0)


@pytest.mark.asyncio
async def test_partial_close_opens_opposite_side_order(client):
    """partial_close on a hedging account = opposite-side market order."""
    instruments = {
        "d": {
            "instruments": [
                {
                    "name": "BTCUSD",
                    "tradableInstrumentId": 206,
                    "routes": [{"id": 9912, "type": "TRADE"}],
                }
            ]
        }
    }
    captured = {}

    def _capture(request):
        captured["body"] = request.read()
        return Response(200, json={"s": "ok", "d": {"orderId": "CLOSE-1"}})

    with respx.mock(base_url=_DEMO_BASE) as r:
        r.get("/trade/accounts/A/instruments").mock(
            return_value=Response(200, json=instruments)
        )
        r.post("/trade/accounts/A/orders").mock(side_effect=_capture)
        result = await client.partial_close(
            account_id="A",
            token="T",
            acc_num="4",
            position_id="POS-X",
            symbol="BTCUSD",
            original_side="buy",
            qty=0.02,
        )
    body = captured["body"].decode()
    assert '"side":"sell"' in body or '"side": "sell"' in body
    assert "0.02" in body
    assert result["order_id"] == "CLOSE-1"


@pytest.mark.asyncio
async def test_partial_close_on_short_uses_buy(client):
    instruments = {
        "d": {
            "instruments": [
                {
                    "name": "ETHUSD",
                    "tradableInstrumentId": 214,
                    "routes": [{"id": 9912, "type": "TRADE"}],
                }
            ]
        }
    }
    captured = {}

    def _capture(request):
        captured["body"] = request.read()
        return Response(200, json={"s": "ok", "d": {"orderId": "CLOSE-2"}})

    with respx.mock(base_url=_DEMO_BASE) as r:
        r.get("/trade/accounts/A/instruments").mock(
            return_value=Response(200, json=instruments)
        )
        r.post("/trade/accounts/A/orders").mock(side_effect=_capture)
        await client.partial_close(
            account_id="A",
            token="T",
            acc_num="4",
            position_id="POS-Y",
            symbol="ETHUSD",
            original_side="sell",
            qty=0.05,
        )
    body = captured["body"].decode()
    assert '"side":"buy"' in body or '"side": "buy"' in body
