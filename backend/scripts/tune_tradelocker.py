"""TradeLocker tuning capture — runs read-only API calls and dumps raw responses to tuning_capture.json so we can verify endpoint shapes.

Run from backend/ with: python scripts/tune_tradelocker.py
Reads creds from .env.tuning (gitignored).
"""
from __future__ import annotations
import asyncio
import json
import os
import sys
from datetime import datetime
from pathlib import Path

import httpx
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env.tuning")

EMAIL = os.environ["TL_DEMO_EMAIL"]
PASSWORD = os.environ["TL_DEMO_PASSWORD"]
SERVER = os.environ["TL_DEMO_SERVER"]
ACC_NUM = os.environ.get("TL_DEMO_ACCOUNT_NUM", "")

CANDIDATES = [
    "https://demo.tradelocker.com/backend-api",
    "https://live.tradelocker.com/backend-api",
]

capture: dict = {"started_at": datetime.utcnow().isoformat() + "Z", "calls": []}


def record(label: str, method: str, url: str, status: int, body, response):
    capture["calls"].append({
        "label": label,
        "method": method,
        "url": url,
        "status": status,
        "request_body_keys": list(body.keys()) if isinstance(body, dict) else None,
        "response_preview": (
            response if isinstance(response, (dict, list))
            else str(response)[:1000]
        ),
    })


async def try_auth(base: str) -> dict | None:
    """Try POST /auth/jwt/token with multiple candidate body shapes."""
    bodies = [
        {"email": EMAIL, "password": PASSWORD, "server": SERVER},
        {"login": EMAIL, "password": PASSWORD, "server": SERVER},
        {"username": EMAIL, "password": PASSWORD, "server": SERVER},
    ]
    async with httpx.AsyncClient(timeout=15.0) as client:
        for body in bodies:
            url = f"{base}/auth/jwt/token"
            try:
                r = await client.post(url, json=body)
            except httpx.HTTPError as e:
                record(f"auth_attempt body_keys={list(body.keys())}", "POST", url, 0, body, str(e))
                continue
            try:
                resp_json = r.json()
            except Exception:
                resp_json = {"raw": r.text[:500]}
            record(f"auth body_keys={list(body.keys())}", "POST", url, r.status_code, body, resp_json)
            if r.status_code in (200, 201):
                return resp_json
    return None


async def try_get(base: str, path: str, token: str, label: str, acc_num: str | None = None):
    url = f"{base}{path}"
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    if acc_num:
        headers["accNum"] = acc_num
    async with httpx.AsyncClient(timeout=15.0) as client:
        try:
            r = await client.get(url, headers=headers)
        except httpx.HTTPError as e:
            record(label, "GET", url, 0, {}, str(e))
            return None
    try:
        resp_json = r.json()
    except Exception:
        resp_json = {"raw": r.text[:500]}
    record(label, "GET", url, r.status_code, {}, resp_json)
    return resp_json if r.status_code in (200, 201) else None


async def main() -> int:
    print(f"Tuning TradeLocker for email={EMAIL} server={SERVER} accNum={ACC_NUM}")
    auth_data = None
    working_base = None
    for base in CANDIDATES:
        print(f"\n--- trying base: {base}")
        auth_data = await try_auth(base)
        if auth_data:
            working_base = base
            print(f"AUTH SUCCESS at {base}")
            break

    if not auth_data:
        print("\n❌ All auth attempts failed. See tuning_capture.json for details.")
        out = ROOT / "tuning_capture.json"
        out.write_text(json.dumps(capture, indent=2))
        print(f"Wrote {out}")
        return 1

    capture["working_base"] = working_base

    access = (
        auth_data.get("accessToken")
        or auth_data.get("access_token")
        or auth_data.get("token")
        or auth_data.get("jwt")
    )
    capture["auth_token_field"] = next(
        (k for k in ("accessToken", "access_token", "token", "jwt") if k in auth_data),
        None,
    )

    if not access:
        print(f"❌ Could not find access token in response keys: {list(auth_data.keys())}")
        out = ROOT / "tuning_capture.json"
        out.write_text(json.dumps(capture, indent=2))
        return 1

    print(f"\n✅ Got access token (field name: {capture['auth_token_field']})")

    # Try to discover the accounts list endpoint
    accounts_paths = [
        "/auth/jwt/all-accounts",
        "/auth/jwt/accounts",
        "/trade/accounts",
        "/auth/accounts",
    ]
    accounts_data = None
    for p in accounts_paths:
        result = await try_get(working_base, p, access, f"accounts_list {p}")
        if result is not None:
            capture["accounts_endpoint"] = p
            accounts_data = result
            break

    # Extract real accountId and accNum from accounts response
    real_id = ACC_NUM
    real_acc_num = ACC_NUM
    if accounts_data:
        accts_list = (
            accounts_data.get("accounts") if isinstance(accounts_data, dict) else accounts_data
        )
        if accts_list:
            first = accts_list[0]
            real_id = str(first.get("id") or first.get("accountId") or ACC_NUM)
            real_acc_num = str(first.get("accNum") or first.get("accountNum") or ACC_NUM)
            capture["resolved_account_id"] = real_id
            capture["resolved_acc_num"] = real_acc_num
            print(f"   resolved id={real_id} accNum={real_acc_num}")

    # Try state endpoint
    state_paths = [
        f"/trade/accounts/{real_id}/state",
        f"/trade/accounts/{real_id}",
        f"/trade/accounts/{real_id}/info",
    ]
    for p in state_paths:
        result = await try_get(working_base, p, access, f"account_state {p}", acc_num=real_acc_num)
        if result is not None:
            capture["state_endpoint"] = p
            break

    # Try positions
    pos_paths = [
        f"/trade/accounts/{real_id}/positions",
        "/trade/positions",
    ]
    for p in pos_paths:
        result = await try_get(working_base, p, access, f"positions {p}", acc_num=real_acc_num)
        if result is not None:
            capture["positions_endpoint"] = p
            break

    # Try instruments list (helps verify symbology)
    instr_paths = [
        f"/trade/accounts/{real_id}/instruments",
        "/trade/instruments",
    ]
    for p in instr_paths:
        result = await try_get(working_base, p, access, f"instruments {p}", acc_num=real_acc_num)
        if result is not None:
            capture["instruments_endpoint"] = p
            break

    # Try orders list
    orders_paths = [
        f"/trade/accounts/{real_id}/orders",
    ]
    for p in orders_paths:
        result = await try_get(working_base, p, access, f"orders {p}", acc_num=real_acc_num)
        if result is not None:
            capture["orders_endpoint"] = p
            break

    # Final capture
    capture["completed_at"] = datetime.utcnow().isoformat() + "Z"
    out = ROOT / "tuning_capture.json"
    out.write_text(json.dumps(capture, indent=2))
    print(f"\n✅ Capture complete → {out}")
    print(f"   Calls made: {len(capture['calls'])}")
    print(f"   Working base: {capture.get('working_base')}")
    print(f"   Auth token field: {capture.get('auth_token_field')}")
    print("   Endpoints discovered:")
    for k in ("accounts_endpoint", "state_endpoint", "positions_endpoint", "instruments_endpoint"):
        print(f"     {k}: {capture.get(k, '❌ not found')}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
