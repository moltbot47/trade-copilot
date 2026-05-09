"""TradeLocker REST API client — verified against Genesis FX demo on 2026-05-08.

Reference: https://public-api.tradelocker.com/

Base URLs:
  - demo: https://demo.tradelocker.com/backend-api
  - live: https://live.tradelocker.com/backend-api

Auth:
  POST /auth/jwt/token  body: {email, password, server}  → 201
  Returns: {accessToken, refreshToken, expireDate}

All /trade/* endpoints require:
  Authorization: Bearer {accessToken}
  accNum: {accNum}     ← NOT the account id; from /auth/jwt/all-accounts response

Genesis FX specifics (verified):
  server="GENFX"
  Account format: id (e.g. "2163244") goes in URL paths,
                  accNum (e.g. "4") goes in header
  Order body uses tradableInstrumentId + routeId (numeric), NOT symbol string.
  Close position: DELETE /trade/positions/{positionId} (not account-scoped).
  Crypto lot step: 0.01 (BTC, ETH).
  Hedging mode: opposite-side orders open new positions (use DELETE to close).
"""
from __future__ import annotations

from typing import Any, Optional

import httpx

from app.config import get_settings


# Account state column indices (verified from /trade/config endpoint)
STATE_COL = {
    "balance": 0,
    "projectedBalance": 1,
    "availableFunds": 2,
    "blockedBalance": 3,
    "cashBalance": 4,
    "unsettledCash": 5,
    "withdrawalAvailable": 6,
    "stocksValue": 7,
    "optionValue": 8,
    "initialMarginReq": 9,
    "maintMarginReq": 10,
    "marginWarningLevel": 11,
    "blockedForStocks": 12,
    "stockOrdersReq": 13,
    "stopOutLevel": 14,
    "warningMarginReq": 15,
    "marginBeforeWarning": 16,
    "todayGross": 17,
    "todayNet": 18,
    "todayFees": 19,
    "todayVolume": 20,
    "todayTradesCount": 21,
    "openGrossPnL": 22,
    "openNetPnL": 23,
    "positionsCount": 24,
    "ordersCount": 25,
}

# Position array indices (verified from /trade/config positionsConfig)
POSITION_COL = {
    "id": 0,
    "tradableInstrumentId": 1,
    "routeId": 2,
    "side": 3,
    "qty": 4,
    "avgPrice": 5,
    "stopLossId": 6,
    "takeProfitId": 7,
    "openDate": 8,
    "unrealizedPl": 9,
    "strategyId": 10,
}


class TradeLockerError(Exception):
    pass


class TradeLockerClient:
    def __init__(self, env: str = "demo", timeout: float = 15.0) -> None:
        s = get_settings()
        self.base_url = (
            s.TRADELOCKER_API_BASE if env == "live" else s.TRADELOCKER_DEMO_API_BASE
        ).rstrip("/")
        self.env = env
        self.timeout = timeout
        # symbol → (tradableInstrumentId, routeId) cache, per-account
        self._instrument_cache: dict[str, dict[str, dict[str, int]]] = {}

    def _headers(self, token: Optional[str] = None, acc_num: Optional[str] = None) -> dict[str, str]:
        h: dict[str, str] = {"Accept": "application/json"}
        if token:
            h["Authorization"] = f"Bearer {token}"
        if acc_num is not None:
            h["accNum"] = str(acc_num)
        return h

    async def _request(
        self,
        method: str,
        path: str,
        *,
        token: Optional[str] = None,
        acc_num: Optional[str] = None,
        json: Optional[dict] = None,
    ) -> Any:
        url = f"{self.base_url}{path}"
        headers = self._headers(token, acc_num)
        async with httpx.AsyncClient(timeout=self.timeout) as c:
            try:
                r = await c.request(method, url, headers=headers, json=json)
            except httpx.HTTPError as e:
                raise TradeLockerError(f"network error: {e}") from e
        if r.status_code == 401:
            raise TradeLockerError("unauthorized (401) - token expired or invalid")
        if r.status_code >= 400:
            raise TradeLockerError(f"{r.status_code} {r.text[:240]}")
        if not r.content:
            return {}
        try:
            return r.json()
        except ValueError:
            return {"raw": r.text}

    # ---------- AUTH ----------
    async def authenticate(self, email: str, password: str, server: str) -> dict:
        """POST /auth/jwt/token. Returns full session data."""
        body = {"email": email, "password": password, "server": server}
        data = await self._request("POST", "/auth/jwt/token", json=body)
        access = data.get("accessToken")
        refresh = data.get("refreshToken")
        if not access:
            raise TradeLockerError(f"auth response missing accessToken: {data}")

        # Fetch the linked accounts so we can store id + accNum
        accts_data = await self._request("GET", "/auth/jwt/all-accounts", token=access)
        accounts = accts_data.get("accounts") or []
        if not accounts:
            raise TradeLockerError("no accounts returned from /auth/jwt/all-accounts")

        first = accounts[0]
        return {
            "access_token": access,
            "refresh_token": refresh,
            "expire_date": data.get("expireDate"),
            "account_id": str(first.get("id")),
            "acc_num": str(first.get("accNum")),
            "balance": float(first.get("accountBalance", 0)),
            "currency": first.get("currency"),
            "all_accounts": accounts,
        }

    async def refresh_access_token(self, refresh_token: str) -> dict:
        """POST /auth/jwt/refresh."""
        data = await self._request(
            "POST", "/auth/jwt/refresh", json={"refreshToken": refresh_token}
        )
        return {
            "access_token": data.get("accessToken"),
            "refresh_token": data.get("refreshToken"),
            "expire_date": data.get("expireDate"),
        }

    # ---------- ACCOUNT ----------
    async def get_account_state(self, account_id: str, token: str, acc_num: str) -> dict:
        """GET /trade/accounts/{id}/state. Returns state with named fields."""
        raw = await self._request(
            "GET", f"/trade/accounts/{account_id}/state", token=token, acc_num=acc_num
        )
        arr = raw.get("d", {}).get("accountDetailsData", [])
        if not arr or len(arr) < len(STATE_COL):
            return {"raw": raw}
        return {name: float(arr[idx]) for name, idx in STATE_COL.items()}

    # ---------- INSTRUMENTS ----------
    async def get_instruments(
        self, account_id: str, token: str, acc_num: str
    ) -> list[dict]:
        """GET /trade/accounts/{id}/instruments — full tradable list."""
        raw = await self._request(
            "GET",
            f"/trade/accounts/{account_id}/instruments",
            token=token,
            acc_num=acc_num,
        )
        return raw.get("d", {}).get("instruments", [])

    async def resolve_symbol(
        self, account_id: str, token: str, acc_num: str, symbol: str
    ) -> tuple[int, int]:
        """Return (tradableInstrumentId, routeId-of-type-TRADE) for a symbol like 'BTCUSD'."""
        cache = self._instrument_cache.setdefault(account_id, {})
        if symbol in cache:
            c = cache[symbol]
            return c["tradableInstrumentId"], c["routeId"]
        instruments = await self.get_instruments(account_id, token, acc_num)
        for inst in instruments:
            if inst.get("name") == symbol:
                trade_route = next(
                    (r for r in inst.get("routes", []) if r.get("type") == "TRADE"),
                    None,
                )
                if not trade_route:
                    raise TradeLockerError(f"no TRADE route for {symbol}")
                tid = inst["tradableInstrumentId"]
                rid = trade_route["id"]
                cache[symbol] = {"tradableInstrumentId": tid, "routeId": rid}
                return tid, rid
        raise TradeLockerError(f"symbol not found: {symbol}")

    # ---------- ORDERS ----------
    async def place_order(
        self,
        account_id: str,
        token: str,
        acc_num: str,
        symbol: str,
        side: str,
        qty: float,
        sl: Optional[float] = None,
        tp: Optional[float] = None,
        order_type: str = "market",
        validity: str = "IOC",
    ) -> dict:
        """POST /trade/accounts/{id}/orders.

        Resolves symbol → (tradableInstrumentId, routeId) and submits the order.
        Returns: {orderId: str, raw: full_response}
        """
        tradable_id, route_id = await self.resolve_symbol(account_id, token, acc_num, symbol)
        body: dict[str, Any] = {
            "tradableInstrumentId": tradable_id,
            "routeId": route_id,
            "qty": qty,
            "side": side.lower(),
            "type": order_type.lower(),
            "validity": validity.upper(),
        }
        if sl is not None:
            body["stopLoss"] = sl
        if tp is not None:
            body["takeProfit"] = tp
        raw = await self._request(
            "POST",
            f"/trade/accounts/{account_id}/orders",
            token=token,
            acc_num=acc_num,
            json=body,
        )
        if raw.get("s") != "ok":
            raise TradeLockerError(
                f"order rejected: {raw.get('errmsg') or raw.get('message') or raw}"
            )
        return {
            "order_id": raw["d"]["orderId"],
            "request_body": body,
            "raw": raw,
        }

    # ---------- POSITIONS ----------
    async def get_positions(self, account_id: str, token: str, acc_num: str) -> list[dict]:
        """GET /trade/accounts/{id}/positions. Returns positions with named fields."""
        raw = await self._request(
            "GET",
            f"/trade/accounts/{account_id}/positions",
            token=token,
            acc_num=acc_num,
        )
        positions = raw.get("d", {}).get("positions", [])
        return [
            {name: row[idx] for name, idx in POSITION_COL.items()}
            for row in positions
        ]

    async def close_position(self, position_id: str, token: str, acc_num: str) -> dict:
        """DELETE /trade/positions/{id}. Returns {s: 'ok'} on success."""
        return await self._request(
            "DELETE",
            f"/trade/positions/{position_id}",
            token=token,
            acc_num=acc_num,
        )

    async def modify_position(
        self,
        position_id: str,
        token: str,
        acc_num: str,
        *,
        stop_loss: Optional[float] = None,
        take_profit: Optional[float] = None,
    ) -> dict:
        """PATCH /trade/positions/{id} — discovered 2026-05-09 on Genesis FX demo.

        Body shape: {"stopLoss": float, "takeProfit": float}. Either field is
        optional but at least one must be provided. Verified against demo
        D#2163244: 200 {"s":"ok"}.

        Other patterns probed (all 4xx): PUT /trade/positions/{id},
        POST /trade/positions/{id}/modify, PATCH under /trade/accounts/{id}/...
        """
        if stop_loss is None and take_profit is None:
            raise TradeLockerError("modify_position requires at least stopLoss or takeProfit")
        body: dict[str, Any] = {}
        if stop_loss is not None:
            body["stopLoss"] = float(stop_loss)
        if take_profit is not None:
            body["takeProfit"] = float(take_profit)
        raw = await self._request(
            "PATCH",
            f"/trade/positions/{position_id}",
            token=token,
            acc_num=acc_num,
            json=body,
        )
        if isinstance(raw, dict) and raw.get("s") and raw["s"] != "ok":
            raise TradeLockerError(
                f"modify rejected: {raw.get('errmsg') or raw.get('message') or raw}"
            )
        return raw

    async def partial_close(
        self,
        account_id: str,
        token: str,
        acc_num: str,
        position_id: str,
        symbol: str,
        original_side: str,
        qty: float,
    ) -> dict:
        """Hedging-mode partial close.

        On Genesis FX hedging accounts there is no first-class partial-close
        endpoint (DELETE always closes 100%, and `closeAmount`/`positionId`
        params are silently ignored — verified 2026-05-09). We emulate it
        by opening a counter-position of `qty` size, which nets the exposure.
        Both legs remain visible on the broker but our cohort accounting
        treats them as netted for P&L.

        Returns the same shape as place_order.
        """
        opposite = "sell" if original_side.lower() == "buy" else "buy"
        return await self.place_order(
            account_id=account_id,
            token=token,
            acc_num=acc_num,
            symbol=symbol,
            side=opposite,
            qty=qty,
        )
