"""Find BTC/ETH symbols and decode account state columns."""
from __future__ import annotations
import asyncio
import json
import os
from pathlib import Path

import httpx
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env.tuning")

EMAIL = os.environ["TL_DEMO_EMAIL"]
PASSWORD = os.environ["TL_DEMO_PASSWORD"]
SERVER = os.environ["TL_DEMO_SERVER"]
BASE = "https://demo.tradelocker.com/backend-api"


async def main() -> int:
    async with httpx.AsyncClient(timeout=20) as c:
        r = await c.post(
            f"{BASE}/auth/jwt/token",
            json={"email": EMAIL, "password": PASSWORD, "server": SERVER},
        )
        token = r.json()["accessToken"]
        print(f"✅ auth ok\n")

        accts = await c.get(
            f"{BASE}/auth/jwt/all-accounts",
            headers={"Authorization": f"Bearer {token}"},
        )
        first = accts.json()["accounts"][0]
        acc_id = first["id"]
        acc_num = first["accNum"]
        print(f"Account: id={acc_id} accNum={acc_num}\n")

        h = {"Authorization": f"Bearer {token}", "accNum": acc_num}

        # Account state COLUMN metadata - this tells us what each index means
        for path in (
            f"/trade/config",
            f"/trade/accounts/{acc_id}/columnConfig",
            f"/trade/accounts/{acc_id}/columns",
        ):
            try:
                r = await c.get(f"{BASE}{path}", headers=h)
                if r.status_code in (200, 201):
                    print(f"✅ {path}")
                    data = r.json()
                    txt = json.dumps(data)
                    if "accountDetails" in txt or "balance" in txt.lower():
                        print(f"   → has accountDetails columns!")
                        Path(ROOT / "tradelocker_config.json").write_text(json.dumps(data, indent=2))
                        print(f"   wrote → tradelocker_config.json")
                        # extract account columns
                        cols = None
                        for k in ("accountDetailsConfig", "accountColumns", "columns"):
                            if isinstance(data.get("d"), dict) and k in data["d"]:
                                cols = data["d"][k]
                                break
                            elif k in data:
                                cols = data[k]
                                break
                        if cols and isinstance(cols, list):
                            print(f"   account state column names ({len(cols)}):")
                            for i, col in enumerate(cols):
                                title = (
                                    col.get("title") if isinstance(col, dict) else col
                                )
                                print(f"     [{i:2}] {title}")
                    break
            except Exception as e:
                pass

        # Crypto search
        print("\n=== CRYPTO INSTRUMENTS ===")
        r = await c.get(f"{BASE}/trade/accounts/{acc_id}/instruments", headers=h)
        data = r.json()
        items = data["d"]["instruments"]
        crypto = [i for i in items if i.get("type") == "CRYPTO"]
        print(f"Total instruments: {len(items)}, crypto: {len(crypto)}\n")
        for it in crypto:
            name = it["name"]
            if any(k in name.upper() for k in ("BTC", "ETH", "XBT")):
                print(
                    f"  {name:12} | id={it['id']:5} tradableId={it['tradableInstrumentId']:5} | {it['description']}"
                )

        # Print STATE response live
        print("\n=== LIVE STATE ===")
        r = await c.get(f"{BASE}/trade/accounts/{acc_id}/state", headers=h)
        data = r.json()
        details = data["d"].get("accountDetailsData", [])
        print(f"raw 26-element accountDetailsData: {details}")

        # Save full crypto list + state for later use
        out = {
            "account_id": acc_id,
            "acc_num": acc_num,
            "crypto_instruments": crypto,
            "account_state_raw": data,
        }
        Path(ROOT / "tradelocker_crypto_and_state.json").write_text(
            json.dumps(out, indent=2)
        )
        print(f"\n✅ saved → tradelocker_crypto_and_state.json")
    return 0


if __name__ == "__main__":
    asyncio.run(main())
