"""Place a 0.001 BTC market buy on demo, capture response, close it cleanly.

Demo only — virtual money.
"""
from __future__ import annotations
import asyncio
import json
import os
import sys
from pathlib import Path

import httpx
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env.tuning")

EMAIL = os.environ["TL_DEMO_EMAIL"]
PASSWORD = os.environ["TL_DEMO_PASSWORD"]
SERVER = os.environ["TL_DEMO_SERVER"]
BASE = "https://demo.tradelocker.com/backend-api"

# BTCUSD specs
BTC_TRADABLE_ID = 206
BTC_ROUTE_ID_TRADE = 9912
TEST_QTY = 0.001  # ~$60 of BTC at $60K price


async def main() -> int:
    captures = {"order_attempts": [], "post_order_state": None, "close_attempts": []}

    async with httpx.AsyncClient(timeout=20) as c:
        # Auth
        r = await c.post(
            f"{BASE}/auth/jwt/token",
            json={"email": EMAIL, "password": PASSWORD, "server": SERVER},
        )
        token = r.json()["accessToken"]
        accts = await c.get(
            f"{BASE}/auth/jwt/all-accounts",
            headers={"Authorization": f"Bearer {token}"},
        )
        first = accts.json()["accounts"][0]
        acc_id = first["id"]
        acc_num = first["accNum"]
        h = {
            "Authorization": f"Bearer {token}",
            "accNum": acc_num,
            "Content-Type": "application/json",
        }
        print(f"✅ auth ok, account id={acc_id} accNum={acc_num} balance=${first['accountBalance']}\n")

        # ----------- ATTEMPT 1: Standard order body -----------
        print(f"--- placing 0.001 BTC market buy (attempt 1) ---")
        body = {
            "tradableInstrumentId": BTC_TRADABLE_ID,
            "routeId": BTC_ROUTE_ID_TRADE,
            "qty": TEST_QTY,
            "side": "buy",
            "type": "market",
            "validity": "IOC",
        }
        r = await c.post(f"{BASE}/trade/accounts/{acc_id}/orders", json=body, headers=h)
        try:
            resp_json = r.json()
        except Exception:
            resp_json = {"raw": r.text[:500]}
        print(f"  status={r.status_code}")
        print(f"  body sent: {body}")
        print(f"  response: {json.dumps(resp_json)[:300]}")
        captures["order_attempts"].append({
            "body": body,
            "status": r.status_code,
            "response": resp_json,
        })

        if r.status_code in (200, 201):
            print("\n✅ order accepted")
            order_data = resp_json.get("d", resp_json)

            # Get state after order
            await asyncio.sleep(2)
            state = await c.get(f"{BASE}/trade/accounts/{acc_id}/state", headers=h)
            captures["post_order_state"] = state.json()
            print(f"\n--- state after order ---")
            details = state.json()["d"].get("accountDetailsData", [])
            print(f"  balance:          ${details[0]}")
            print(f"  availableFunds:   ${details[2]}")
            print(f"  openGrossPnL:     ${details[22]}")
            print(f"  positionsCount:   {details[24]}")

            # Get positions
            pos = await c.get(
                f"{BASE}/trade/accounts/{acc_id}/positions", headers=h
            )
            captures["positions_after"] = pos.json()
            positions = pos.json()["d"].get("positions", [])
            print(f"\n--- positions ({len(positions)}) ---")
            for p in positions:
                print(f"  {p}")

            if positions:
                # Close the first position by placing opposite-side market order
                print(f"\n--- closing position ---")
                first_pos = positions[0]
                # positions are arrays per columns: [id, tradableInstrumentId, routeId, side, qty, avgPrice, sl, tp, openDate, unrealizedPl, strategyId]
                pos_id = first_pos[0] if isinstance(first_pos, list) else first_pos.get("id")
                pos_qty = (
                    first_pos[4] if isinstance(first_pos, list) else first_pos.get("qty")
                )
                # Try direct close endpoint first
                close_paths = [
                    (f"/trade/positions/{pos_id}", "DELETE", None),
                    (
                        f"/trade/accounts/{acc_id}/orders",
                        "POST",
                        {
                            "tradableInstrumentId": BTC_TRADABLE_ID,
                            "routeId": BTC_ROUTE_ID_TRADE,
                            "qty": float(pos_qty),
                            "side": "sell",
                            "type": "market",
                            "validity": "IOC",
                            "positionId": pos_id,
                        },
                    ),
                ]
                for path, method, cbody in close_paths:
                    try:
                        if method == "DELETE":
                            r = await c.delete(f"{BASE}{path}", headers=h)
                        else:
                            r = await c.post(f"{BASE}{path}", json=cbody, headers=h)
                        print(f"  {method} {path} → {r.status_code} {r.text[:200]}")
                        captures["close_attempts"].append({
                            "method": method,
                            "path": path,
                            "body": cbody,
                            "status": r.status_code,
                            "response_text": r.text[:300],
                        })
                        if r.status_code in (200, 201, 204):
                            print(f"\n  ✅ position closed via {method} {path}")
                            break
                    except Exception as e:
                        print(f"  {method} {path} → ERROR {e}")
        else:
            print("\n❌ order rejected — capturing details for debugging")

        Path(ROOT / "tradelocker_order_test.json").write_text(json.dumps(captures, indent=2))
        print(f"\n✅ saved → tradelocker_order_test.json")

    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
