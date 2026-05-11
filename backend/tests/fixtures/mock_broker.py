"""MockBroker — drop-in replacement for TradeLockerClient in tests.

Implements the same async API surface as TradeLockerClient but operates
on in-memory state. Records all orders for later inspection, simulates
fills, supports modify/close/partial-close, and can be driven through
arbitrary scenarios without hitting a real broker.

Usage:
    broker = MockBroker(env="demo")
    broker.set_quote("BTCUSD", bid=80000, ask=80020)
    # Pass it where a TradeLockerClient is expected:
    bar_fetcher = BarFetcher(client=broker, ...)
    await broker.place_order(account_id="1", ...)
    assert broker.orders_placed == 1

The fixture deliberately mirrors the production client's return shapes
and error semantics so tests catch real-world contract drift.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from app.core.tradelocker_client import TradeLockerError


@dataclass
class MockPosition:
    id: str
    side: str
    qty: float
    entry_price: float
    stop_loss: Optional[float]
    take_profit: Optional[float]
    tradable_instrument_id: int
    order_id: str
    open_date: int
    closed: bool = False


@dataclass
class MockOrder:
    order_id: str
    side: str
    qty: float
    symbol: str
    sl: Optional[float]
    tp: Optional[float]
    client_order_id: Optional[str]


@dataclass
class MockBroker:
    env: str = "demo"
    base_url: str = "mock://broker"
    # symbol → (tradable_instrument_id, route_id)
    _symbol_map: dict[str, tuple[int, int]] = field(default_factory=dict)
    # symbol → (bid, ask)
    _quotes: dict[str, tuple[float, float]] = field(default_factory=dict)
    _positions: list[MockPosition] = field(default_factory=list)
    orders: list[MockOrder] = field(default_factory=list)
    _next_order_seq: int = 1
    _next_position_seq: int = 1
    _failures: dict[str, int] = field(default_factory=dict)  # endpoint → remaining failures
    _account_state: dict[str, float] = field(
        default_factory=lambda: {
            "balance": 10000.0,
            "projectedBalance": 10000.0,
            "availableFunds": 10000.0,
            "todayNet": 0.0,
            "openGrossPnL": 0.0,
            "positionsCount": 0,
        }
    )

    # --------------- setup helpers (test-only API) ---------------

    def set_quote(self, symbol: str, bid: float, ask: float) -> None:
        self._quotes[symbol] = (bid, ask)
        if symbol not in self._symbol_map:
            tid = 1000 + len(self._symbol_map)
            self._symbol_map[symbol] = (tid, tid + 1)  # route_id offset by 1

    def set_account_state(self, **kwargs: float) -> None:
        self._account_state.update(kwargs)

    def queue_failure(self, endpoint: str, n: int = 1) -> None:
        """Next N calls to `endpoint` will raise TradeLockerError."""
        self._failures[endpoint] = self._failures.get(endpoint, 0) + n

    def _maybe_fail(self, endpoint: str) -> None:
        if self._failures.get(endpoint, 0) > 0:
            self._failures[endpoint] -= 1
            raise TradeLockerError(f"mock failure on {endpoint}")

    @property
    def orders_placed(self) -> int:
        return len(self.orders)

    @property
    def open_positions(self) -> list[MockPosition]:
        return [p for p in self._positions if not p.closed]

    # --------------- TradeLockerClient-compatible async API ---------------

    async def resolve_symbol(
        self, account_id: str, token: str, acc_num: str, symbol: str
    ) -> tuple[int, int]:
        self._maybe_fail("resolve_symbol")
        if symbol not in self._symbol_map:
            self.set_quote(symbol, bid=80000.0, ask=80020.0)
        return self._symbol_map[symbol]

    async def get_instruments(
        self, account_id: str, token: str, acc_num: str
    ) -> list[dict]:
        self._maybe_fail("get_instruments")
        return [
            {
                "name": sym,
                "tradableInstrumentId": tid,
                "routes": [
                    {"id": rid, "type": "TRADE"},
                    {"id": rid + 100, "type": "INFO"},
                ],
            }
            for sym, (tid, rid) in self._symbol_map.items()
        ]

    async def get_account_state(
        self, account_id: str, token: str, acc_num: str
    ) -> dict:
        self._maybe_fail("get_account_state")
        st = dict(self._account_state)
        st["positionsCount"] = float(len(self.open_positions))
        return st

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
        self._maybe_fail("place_order")

        # Honor client_order_id dedup — match production behavior
        if client_order_id:
            for o in self.orders:
                if o.client_order_id == client_order_id:
                    return {
                        "order_id": o.order_id,
                        "duplicate": True,
                        "idempotency_key": client_order_id,
                        "raw": {"s": "ok"},
                    }

        tid, _ = await self.resolve_symbol(account_id, token, acc_num, symbol)
        bid, ask = self._quotes.get(symbol, (80000.0, 80020.0))
        entry = ask if side == "buy" else bid

        order_id = f"MOCK-ORD-{self._next_order_seq}"
        self._next_order_seq += 1
        order = MockOrder(
            order_id=order_id,
            side=side,
            qty=qty,
            symbol=symbol,
            sl=sl,
            tp=tp,
            client_order_id=client_order_id,
        )
        self.orders.append(order)

        pos = MockPosition(
            id=f"MOCK-POS-{self._next_position_seq}",
            side=side,
            qty=qty,
            entry_price=entry,
            stop_loss=sl,
            take_profit=tp,
            tradable_instrument_id=tid,
            order_id=order_id,
            open_date=self._next_position_seq,
        )
        self._next_position_seq += 1
        self._positions.append(pos)

        return {
            "order_id": order_id,
            "request_body": {"clientOrderId": client_order_id} if client_order_id else {},
            "raw": {"s": "ok", "d": {"orderId": order_id}},
            "idempotency_key": client_order_id,
            "duplicate": False,
        }

    async def get_positions(
        self, account_id: str, token: str, acc_num: str
    ) -> list[dict]:
        self._maybe_fail("get_positions")
        return [
            {
                "id": p.id,
                "side": p.side,
                "qty": p.qty,
                "avgPrice": p.entry_price,
                "tradableInstrumentId": p.tradable_instrument_id,
                "stopLoss": p.stop_loss,
                "takeProfit": p.take_profit,
                "openDate": p.open_date,
                "orderId": p.order_id,
                "unrealizedPl": 0.0,
            }
            for p in self.open_positions
        ]

    async def close_position(
        self, position_id: str, token: str, acc_num: str
    ) -> dict:
        self._maybe_fail("close_position")
        for p in self._positions:
            if p.id == position_id and not p.closed:
                p.closed = True
                return {"s": "ok"}
        raise TradeLockerError(f"position {position_id} not found")

    async def modify_position(
        self,
        position_id: str,
        token: str,
        acc_num: str,
        stop_loss: Optional[float] = None,
        take_profit: Optional[float] = None,
    ) -> dict:
        self._maybe_fail("modify_position")
        if stop_loss is None and take_profit is None:
            raise TradeLockerError("modify_position requires at least stopLoss or takeProfit")
        for p in self._positions:
            if p.id == position_id and not p.closed:
                if stop_loss is not None:
                    p.stop_loss = stop_loss
                if take_profit is not None:
                    p.take_profit = take_profit
                return {"s": "ok"}
        raise TradeLockerError(f"position {position_id} not found")

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
        """Hedging-mode emulation — opens an opposite-side market order
        of qty, returns the new opposing position's order_id."""
        self._maybe_fail("partial_close")
        opposite = "sell" if original_side == "buy" else "buy"
        return await self.place_order(
            account_id=account_id,
            token=token,
            acc_num=acc_num,
            symbol=symbol,
            side=opposite,
            qty=qty,
        )

    # --------------- utility for tests ---------------

    def trigger_sl(self, position_id: str) -> None:
        """Simulate price moving through SL — closes the position at SL price."""
        for p in self._positions:
            if p.id == position_id and not p.closed and p.stop_loss is not None:
                p.closed = True
                # Update quote so subsequent get_positions reflects close
                return
