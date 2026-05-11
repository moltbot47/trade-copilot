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
    # Class-level idempotency cache shared across instances. TradeLocker
    # tokens rotate but client_order_ids should still dedup if the same key
    # comes through a new client object during a retry.
    _IDEMPOTENCY_TTL_SECONDS = 60.0
    _idempotency_cache: dict[str, dict[str, Any]] = {}

    def __init__(self, env: str = "demo", timeout: float = 15.0) -> None:
        s = get_settings()
        self.base_url = (
            s.TRADELOCKER_API_BASE if env == "live" else s.TRADELOCKER_DEMO_API_BASE
        ).rstrip("/")
        self.env = env
        self.timeout = timeout
        # symbol → (tradableInstrumentId, routeId) cache, per-account
        self._instrument_cache: dict[str, dict[str, dict[str, int]]] = {}

    def _idempotency_cache_hit(self, key: str) -> bool:
        import time as _time

        rec = self._idempotency_cache.get(key)
        if not rec:
            return False
        if _time.monotonic() - rec["ts"] > self._IDEMPOTENCY_TTL_SECONDS:
            self._idempotency_cache.pop(key, None)
            return False
        return True

    def _idempotency_cache_put(self, key: str, response: dict[str, Any]) -> None:
        import time as _time

        self._idempotency_cache[key] = {"ts": _time.monotonic(), "response": response}
        # Opportunistic eviction: keep cache bounded
        if len(self._idempotency_cache) > 256:
            cutoff = _time.monotonic() - self._IDEMPOTENCY_TTL_SECONDS
            for k, v in list(self._idempotency_cache.items()):
                if v["ts"] < cutoff:
                    self._idempotency_cache.pop(k, None)

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
        extra_headers: Optional[dict[str, str]] = None,
    ) -> Any:
        url = f"{self.base_url}{path}"
        headers = self._headers(token, acc_num)
        if extra_headers:
            headers.update(extra_headers)
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

    async def list_all_accounts(self, token: str) -> list[dict]:
        """GET /auth/jwt/all-accounts using an existing access token.

        Used by the switch-account flow so the user doesn't have to re-type
        credentials just to see what accounts they have. Returns the same
        shape `authenticate()` returns under the `all_accounts` key.
        """
        data = await self._request("GET", "/auth/jwt/all-accounts", token=token)
        return data.get("accounts") or []

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
        client_order_id: Optional[str] = None,
    ) -> dict:
        """POST /trade/accounts/{id}/orders.

        Resolves symbol → (tradableInstrumentId, routeId) and submits the order.
        If ``client_order_id`` is provided, it's sent as ``clientOrderId`` in
        the request body and also via the ``X-Idempotency-Key`` header. The
        client also short-circuits duplicate sends within the in-process
        dedup window (60s) — if the same key was just placed, returns the
        cached response without hitting the broker. Prevents the same signal
        from creating 2 positions during retry storms.

        Returns: {orderId: str, raw: full_response, idempotency_key: str|None,
                  duplicate: bool (true if a deduped retry)}
        """
        # In-process dedup: any retry with the same key within 60s returns
        # the cached response without re-submitting. Broker may also dedup
        # if it honors X-Idempotency-Key, but we don't depend on it.
        if client_order_id and self._idempotency_cache_hit(client_order_id):
            cached = self._idempotency_cache[client_order_id]["response"]
            return {**cached, "duplicate": True}

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
        if client_order_id:
            body["clientOrderId"] = client_order_id

        extra_headers = {"X-Idempotency-Key": client_order_id} if client_order_id else None
        raw = await self._request(
            "POST",
            f"/trade/accounts/{account_id}/orders",
            token=token,
            acc_num=acc_num,
            json=body,
            extra_headers=extra_headers,
        )
        if raw.get("s") != "ok":
            raise TradeLockerError(
                f"order rejected: {raw.get('errmsg') or raw.get('message') or raw}"
            )
        response = {
            "order_id": raw["d"]["orderId"],
            "request_body": body,
            "raw": raw,
            "idempotency_key": client_order_id,
            "duplicate": False,
        }
        if client_order_id:
            self._idempotency_cache_put(client_order_id, response)
        return response

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
