"""Unit tests for TradeLockerClient using respx for httpx mocking."""
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
async def test_authenticate_201_response_handled(client):
    """TradeLocker /auth/jwt/token returns 201 (not 200) — verify it's accepted."""
    auth_payload = {
        "accessToken": "ACCESS_TOK",
        "refreshToken": "REFRESH_TOK",
        "expireDate": "2099-01-01T00:00:00Z",
    }
    accts_payload = {
        "accounts": [
            {
                "id": "2163244",
                "accNum": "4",
                "accountBalance": 12_500.0,
                "currency": "USD",
            }
        ]
    }
    with respx.mock(base_url=_DEMO_BASE) as r:
        r.post("/auth/jwt/token").mock(return_value=Response(201, json=auth_payload))
        r.get("/auth/jwt/all-accounts").mock(return_value=Response(200, json=accts_payload))
        result = await client.authenticate("e@x.com", "pw", "GENFX")

    assert result["access_token"] == "ACCESS_TOK"
    assert result["refresh_token"] == "REFRESH_TOK"
    assert result["account_id"] == "2163244"
    assert result["acc_num"] == "4"
    assert result["balance"] == 12_500.0


@pytest.mark.asyncio
async def test_authenticate_posts_correct_body_shape(client):
    auth_payload = {"accessToken": "TOK", "refreshToken": "RT", "expireDate": "2099"}
    accts = {"accounts": [{"id": "1", "accNum": "1", "accountBalance": 0, "currency": "USD"}]}
    captured = {}

    def _capture(request):
        captured["body"] = request.read()
        return Response(201, json=auth_payload)

    with respx.mock(base_url=_DEMO_BASE) as r:
        r.post("/auth/jwt/token").mock(side_effect=_capture)
        r.get("/auth/jwt/all-accounts").mock(return_value=Response(200, json=accts))
        await client.authenticate("foo@bar.com", "p", "GENFX")

    body = captured["body"].decode()
    assert "foo@bar.com" in body
    assert "GENFX" in body
    # Don't assert the password value but the field is expected.
    assert "password" in body


@pytest.mark.asyncio
async def test_authenticate_wrong_credentials_raises(client):
    with respx.mock(base_url=_DEMO_BASE) as r:
        r.post("/auth/jwt/token").mock(return_value=Response(401, json={"error": "bad creds"}))
        with pytest.raises(TradeLockerError):
            await client.authenticate("e@x.com", "wrong", "GENFX")


@pytest.mark.asyncio
async def test_get_account_state_column_array_to_named_dict(client):
    # 26 columns matching STATE_COL definition
    arr = [float(i) for i in range(26)]
    raw = {"d": {"accountDetailsData": arr}}
    with respx.mock(base_url=_DEMO_BASE) as r:
        r.get("/trade/accounts/2163244/state").mock(return_value=Response(200, json=raw))
        state = await client.get_account_state("2163244", token="TOK", acc_num="4")
    assert state["balance"] == 0.0
    assert state["projectedBalance"] == 1.0
    assert state["availableFunds"] == 2.0
    assert state["positionsCount"] == 24.0
    assert state["ordersCount"] == 25.0


@pytest.mark.asyncio
async def test_place_order_body_includes_required_fields(client):
    """Order body must include tradableInstrumentId, routeId, and qty."""
    instruments_payload = {
        "d": {
            "instruments": [
                {
                    "name": "BTCUSD",
                    "tradableInstrumentId": 4242,
                    "routes": [{"id": 7, "type": "TRADE"}, {"id": 8, "type": "INFO"}],
                }
            ]
        }
    }
    place_response = {"s": "ok", "d": {"orderId": "ORD-123"}}
    captured = {}

    def _capture_order(request):
        captured["body"] = request.read()
        return Response(200, json=place_response)

    with respx.mock(base_url=_DEMO_BASE) as r:
        r.get("/trade/accounts/A/instruments").mock(
            return_value=Response(200, json=instruments_payload)
        )
        r.post("/trade/accounts/A/orders").mock(side_effect=_capture_order)

        result = await client.place_order(
            account_id="A",
            token="TOK",
            acc_num="1",
            symbol="BTCUSD",
            side="buy",
            qty=0.05,
        )

    body = captured["body"].decode()
    assert "tradableInstrumentId" in body
    assert "4242" in body
    assert "routeId" in body
    assert '"qty":0.05' in body or '"qty": 0.05' in body
    assert result["order_id"] == "ORD-123"


@pytest.mark.asyncio
async def test_resolve_symbol_caches_results(client):
    """A second call to resolve_symbol must NOT re-hit /instruments."""
    instruments_payload = {
        "d": {
            "instruments": [
                {
                    "name": "ETHUSD",
                    "tradableInstrumentId": 99,
                    "routes": [{"id": 5, "type": "TRADE"}],
                }
            ]
        }
    }
    with respx.mock(base_url=_DEMO_BASE) as r:
        route = r.get("/trade/accounts/A/instruments").mock(
            return_value=Response(200, json=instruments_payload)
        )
        tid1, rid1 = await client.resolve_symbol("A", "TOK", "1", "ETHUSD")
        tid2, rid2 = await client.resolve_symbol("A", "TOK", "1", "ETHUSD")

    assert (tid1, rid1) == (99, 5)
    assert (tid2, rid2) == (99, 5)
    assert route.call_count == 1, "instruments endpoint should only be hit once"


@pytest.mark.asyncio
async def test_resolve_symbol_not_found_raises(client):
    instruments_payload = {"d": {"instruments": []}}
    with respx.mock(base_url=_DEMO_BASE) as r:
        r.get("/trade/accounts/A/instruments").mock(
            return_value=Response(200, json=instruments_payload)
        )
        with pytest.raises(TradeLockerError):
            await client.resolve_symbol("A", "TOK", "1", "NOSUCH")


@pytest.mark.asyncio
async def test_close_position_uses_delete(client):
    """close_position must hit DELETE /trade/positions/{id}."""
    with respx.mock(base_url=_DEMO_BASE) as r:
        del_route = r.delete("/trade/positions/POS-42").mock(
            return_value=Response(200, json={"s": "ok"})
        )
        result = await client.close_position("POS-42", token="TOK", acc_num="1")
    assert del_route.called
    assert result["s"] == "ok"


@pytest.mark.asyncio
async def test_401_response_raises_with_message(client):
    with respx.mock(base_url=_DEMO_BASE) as r:
        r.get("/trade/accounts/X/state").mock(return_value=Response(401, text="nope"))
        with pytest.raises(TradeLockerError) as exc:
            await client.get_account_state("X", token="bad", acc_num="1")
    assert "unauthorized" in str(exc.value).lower() or "401" in str(exc.value)
