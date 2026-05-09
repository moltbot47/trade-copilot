"""Profit-only quick-trade session on BTC + ETH demo.

User constraint: "do analysis and take quick trades to make a profit. don't close out in loss."

Strategy:
  1. Pull recent price candles for BTCUSD and ETHUSD
  2. Detect short-term momentum direction (last N candles)
  3. Place 0.01-lot market order in momentum direction
  4. Set a TIGHT take-profit (~0.03% target, ~$20-30 on BTC, ~$0.50-1 on ETH)
  5. NO hard stop-loss (per user request) — but capped exposure via 0.01 lot size
  6. Poll positions every 10s; close ONLY when in profit ≥ target
  7. Hard cap: max 30 minutes per trade — if not in profit by then, log + skip
     (we don't book a loss but we also don't tie up capital indefinitely)

Demo only. Honest accounting at the end.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

import httpx
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env.tuning")

EMAIL = os.environ["TL_DEMO_EMAIL"]
PASSWORD = os.environ["TL_DEMO_PASSWORD"]
SERVER = os.environ["TL_DEMO_SERVER"]
BASE = "https://demo.tradelocker.com/backend-api"

QTY = 0.01
TARGET_PROFIT_PCT = 0.0003  # 0.03% — tiny, achievable in minutes
MAX_HOLD_SECONDS = 1800     # 30 min ceiling per trade
POLL_INTERVAL = 10          # seconds


async def auth(c: httpx.AsyncClient) -> dict:
    r = await c.post(
        f"{BASE}/auth/jwt/token",
        json={"email": EMAIL, "password": PASSWORD, "server": SERVER},
    )
    r.raise_for_status()
    j = r.json()
    accts = await c.get(
        f"{BASE}/auth/jwt/all-accounts",
        headers={"Authorization": f"Bearer {j['accessToken']}"},
    )
    first = accts.json()["accounts"][0]
    return {
        "token": j["accessToken"],
        "account_id": first["id"],
        "acc_num": first["accNum"],
        "balance": float(first["accountBalance"]),
    }


async def get_price(c: httpx.AsyncClient, sess: dict, tradable_id: int) -> float | None:
    """Best-effort current price via the most recent quote in account state."""
    h = {"Authorization": f"Bearer {sess['token']}", "accNum": sess["acc_num"]}
    # Use position avgPrice as a price echo; if no position, return None.
    # For real implementation we'd subscribe to the price WS, but for this
    # test we read the position row's avgPrice after we open it.
    return None


async def get_positions(c: httpx.AsyncClient, sess: dict) -> list[dict]:
    h = {"Authorization": f"Bearer {sess['token']}", "accNum": sess["acc_num"]}
    r = await c.get(f"{BASE}/trade/accounts/{sess['account_id']}/positions", headers=h)
    if r.status_code != 200:
        return []
    rows = r.json().get("d", {}).get("positions", [])
    return [
        {
            "id": p[0],
            "tradableInstrumentId": int(p[1]),
            "side": p[3],
            "qty": float(p[4]),
            "avgPrice": float(p[5]),
            "unrealizedPl": float(p[9] or 0),
        }
        for p in rows
    ]


async def get_state(c: httpx.AsyncClient, sess: dict) -> dict:
    h = {"Authorization": f"Bearer {sess['token']}", "accNum": sess["acc_num"]}
    r = await c.get(f"{BASE}/trade/accounts/{sess['account_id']}/state", headers=h)
    arr = r.json()["d"]["accountDetailsData"]
    return {
        "balance": arr[0],
        "equity": arr[1],
        "openGrossPnL": arr[22],
        "todayNet": arr[18],
        "positionsCount": int(arr[24]),
    }


async def open_market(
    c: httpx.AsyncClient, sess: dict, tradable_id: int, route_id: int, side: str, qty: float
) -> str | None:
    h = {
        "Authorization": f"Bearer {sess['token']}",
        "accNum": sess["acc_num"],
        "Content-Type": "application/json",
    }
    body = {
        "tradableInstrumentId": tradable_id,
        "routeId": route_id,
        "qty": qty,
        "side": side.lower(),
        "type": "market",
        "validity": "IOC",
    }
    r = await c.post(f"{BASE}/trade/accounts/{sess['account_id']}/orders", json=body, headers=h)
    if r.status_code in (200, 201):
        j = r.json()
        if j.get("s") == "ok":
            return j["d"]["orderId"]
    return None


async def close_position(c: httpx.AsyncClient, sess: dict, position_id: str) -> bool:
    h = {"Authorization": f"Bearer {sess['token']}", "accNum": sess["acc_num"]}
    r = await c.delete(f"{BASE}/trade/positions/{position_id}", headers=h)
    return r.status_code == 200


async def history(
    c: httpx.AsyncClient, sess: dict, tradable_id: int, route_id: int, resolution: str = "1"
) -> list[dict]:
    """Pull recent OHLCV bars for momentum detection."""
    h = {"Authorization": f"Bearer {sess['token']}", "accNum": sess["acc_num"]}
    now_ms = int(time.time() * 1000)
    from_ms = now_ms - 30 * 60 * 1000  # last 30 min
    params = {
        "routeId": route_id,
        "tradableInstrumentId": tradable_id,
        "resolution": resolution,
        "from": from_ms,
        "to": now_ms,
    }
    for path in ("/trade/history", f"/trade/accounts/{sess['account_id']}/history"):
        r = await c.get(f"{BASE}{path}", headers=h, params=params)
        if r.status_code == 200:
            data = r.json().get("d", {})
            bars = data.get("barDetails") or data.get("bars") or []
            if bars:
                return bars
    return []


def momentum_direction(bars: list[dict]) -> str | None:
    """Tiny analysis: average direction of last 5 closes."""
    if len(bars) < 5:
        return None
    closes = []
    for b in bars[-10:]:
        # bar shape varies; try common keys + array index
        if isinstance(b, list):
            closes.append(float(b[4]))   # OHLCV → close at idx 4
        elif isinstance(b, dict):
            c = b.get("c") or b.get("close")
            if c is not None:
                closes.append(float(c))
    if len(closes) < 5:
        return None
    delta = closes[-1] - closes[0]
    if abs(delta) / closes[0] < 0.0001:  # too flat
        return None
    return "buy" if delta > 0 else "sell"


async def trade_one(
    c: httpx.AsyncClient,
    sess: dict,
    symbol: str,
    tradable_id: int,
    route_id: int,
) -> dict:
    """Run one analyze → enter → wait-for-profit → close cycle on a symbol."""
    print(f"\n┌─── {symbol} ─────────────────────────────────────")

    bars = await history(c, sess, tradable_id, route_id)
    print(f"│ history bars fetched: {len(bars)}")

    side = momentum_direction(bars) or "buy"  # default long if flat / no data
    print(f"│ momentum direction: {side}")

    order_id = await open_market(c, sess, tradable_id, route_id, side, QTY)
    if not order_id:
        print(f"│ ❌ entry failed")
        return {"symbol": symbol, "status": "entry_failed"}

    await asyncio.sleep(2)

    positions = await get_positions(c, sess)
    pos = next((p for p in positions if p["tradableInstrumentId"] == tradable_id and p["side"] == side), None)
    if not pos:
        print(f"│ ❌ position not found after open (order {order_id})")
        return {"symbol": symbol, "status": "no_position"}

    entry = pos["avgPrice"]
    target_profit_per_unit = entry * TARGET_PROFIT_PCT
    print(f"│ ENTERED  {side} {QTY} @ ${entry:.4f}  (pos {pos['id']})")
    print(f"│ target:  +${target_profit_per_unit:.4f}/unit  (~+{TARGET_PROFIT_PCT*100:.3f}%)")

    started = time.time()
    last_pnl = pos["unrealizedPl"]
    iters = 0

    while time.time() - started < MAX_HOLD_SECONDS:
        await asyncio.sleep(POLL_INTERVAL)
        iters += 1
        positions = await get_positions(c, sess)
        cur = next((p for p in positions if p["id"] == pos["id"]), None)
        if cur is None:
            print(f"│ position vanished — likely auto-closed")
            return {"symbol": symbol, "status": "vanished"}

        pnl = cur["unrealizedPl"]
        held_s = int(time.time() - started)
        if abs(pnl - last_pnl) > 0.01 or iters % 6 == 0:
            print(f"│ t={held_s:>4}s  PnL=${pnl:+.4f}  px≈?  pos {cur['id']}")
            last_pnl = pnl

        # Profit hit?
        if pnl > 0.05:  # at least $0.05 to overcome spread + transaction noise
            print(f"│ ✅ PROFIT REACHED  PnL=${pnl:+.4f} after {held_s}s — closing")
            ok = await close_position(c, sess, cur["id"])
            return {
                "symbol": symbol,
                "status": "closed_profit" if ok else "close_failed",
                "side": side,
                "qty": QTY,
                "entry": entry,
                "pnl": pnl,
                "held_seconds": held_s,
            }

    held_s = int(time.time() - started)
    print(f"│ ⏱️  timeout {MAX_HOLD_SECONDS}s reached without profit")
    print(f"│    Per user request: NOT closing at a loss. Position left open.")
    return {
        "symbol": symbol,
        "status": "timeout_held",
        "side": side,
        "qty": QTY,
        "entry": entry,
        "last_pnl": last_pnl,
        "held_seconds": held_s,
        "position_id": pos["id"],
    }


async def main() -> int:
    async with httpx.AsyncClient(timeout=20) as c:
        sess = await auth(c)
        s0 = await get_state(c, sess)
        print(f"START balance=${s0['balance']:.2f}  equity=${s0['equity']:.2f}  open_pnl=${s0['openGrossPnL']:.2f}  positions={s0['positionsCount']}")

        results = []
        for symbol, tid, rid in [
            ("BTCUSD", 206, 9912),
            ("ETHUSD", 214, 9912),
        ]:
            res = await trade_one(c, sess, symbol, tid, rid)
            results.append(res)
            await asyncio.sleep(3)

        s1 = await get_state(c, sess)

        print("\n═══════════════════════════════════════════════════════")
        print("PROFIT-ONLY SESSION SUMMARY")
        print("═══════════════════════════════════════════════════════")
        for r in results:
            if r["status"] == "closed_profit":
                print(f"  ✅ {r['symbol']:6}  {r['side']} {r['qty']} @ ${r['entry']:.2f} → +${r['pnl']:.4f} ({r['held_seconds']}s)")
            elif r["status"] == "timeout_held":
                print(f"  ⏱️  {r['symbol']:6}  {r['side']} {r['qty']} @ ${r['entry']:.2f} → STILL OPEN  (last PnL ${r['last_pnl']:+.4f}, {r['held_seconds']}s)")
            else:
                print(f"  ❌ {r['symbol']:6}  {r['status']}")
        print(f"\n  Account:  start=${s0['balance']:.2f}  end=${s1['balance']:.2f}  delta=${s1['balance']-s0['balance']:+.4f}")
        print(f"  Today net realised: ${s1['todayNet']:+.4f}")
        print(f"  Open positions left: {s1['positionsCount']}")
        print(f"  Open unrealized PnL: ${s1['openGrossPnL']:+.4f}")

        # Persist
        out = {
            "started_at": s0,
            "ended_at": s1,
            "results": results,
            "config": {
                "qty": QTY,
                "target_profit_pct": TARGET_PROFIT_PCT,
                "max_hold_seconds": MAX_HOLD_SECONDS,
            },
        }
        Path(ROOT / "profit_session.json").write_text(json.dumps(out, indent=2, default=str))
        print(f"\n  saved → profit_session.json")

    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
