"""End-to-end test of the LaT-PFN Quant Trader runner via /api/strategy.

Drives the full lifecycle through the public HTTP API:

  1. Login → JWT cookie
  2. POST /api/strategy/start  (bot_id=5, latpfn_quant)
  3. Poll /api/strategy/status until runner_alive=True / state.is_running=True
  4. Optionally inspect open broker positions (cleanup safeguard)
  5. POST /api/strategy/stop
  6. Verify runner_alive flips back to False
  7. Print structured summary

Usage:
    # Local dev server on http://localhost:8000
    python scripts/test_quant_runner_e2e.py

    # Production
    python scripts/test_quant_runner_e2e.py --prod

Env:
    E2E_LOGIN_EMAIL      e.g. butler135@gmail.com (default)
    TC_SESSION           pre-existing session cookie (use with --skip-login)

Optional:
    E2E_TIMEFRAME (default 1m)
    E2E_BOT_ID    (default 5)
    E2E_SYMBOLS   (csv, default BTCUSD)
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime
from typing import Any

import httpx

LOCAL_BASE = "http://localhost:8000"
PROD_BASE = "https://api.trading.jetlag-recovery.com"


def _now_iso() -> str:
    return datetime.utcnow().isoformat() + "Z"


def _print(label: str, msg: str = "") -> None:
    print(f"[{_now_iso()}] {label} {msg}".rstrip())


def login(client: httpx.Client, email: str) -> None:
    """POST /api/auth/login → sets tc_session cookie on the client.

    The trade-copilot login endpoint is email-only (MVP) — first-time
    emails auto-create a User row. The session cookie is set on the
    response and propagated automatically by httpx.Client.
    """
    res = client.post(
        "/api/auth/login",
        json={"email": email},
        timeout=20.0,
    )
    if res.status_code != 200:
        raise RuntimeError(f"login failed: {res.status_code} {res.text}")
    if "tc_session" not in client.cookies:
        # fallback: maybe API returns a token in body
        body = res.json() if res.headers.get("content-type", "").startswith("application/json") else {}
        token = body.get("access_token") or body.get("token")
        if token:
            client.headers["Authorization"] = f"Bearer {token}"
        else:
            raise RuntimeError("login OK but no session cookie/token returned")
    _print("LOGIN", f"ok as {email}")


def start_strategy(
    client: httpx.Client, bot_id: int, timeframe: str, symbols: list[str], emails: list[str]
) -> dict:
    res = client.post(
        "/api/strategy/start",
        json={
            "bot_id": bot_id,
            "timeframe": timeframe,
            "symbols": symbols,
            "user_emails": emails,
        },
        timeout=30.0,
    )
    if res.status_code != 200:
        raise RuntimeError(f"start failed: {res.status_code} {res.text}")
    body = res.json()
    _print("START", f"bot={bot_id} tf={timeframe} → {body.get('status')} runner={body.get('runner_type')}")
    return body


def fetch_status(client: httpx.Client, bot_id: int, timeframe: str) -> dict:
    res = client.get(f"/api/strategy/status?bot_id={bot_id}&timeframe={timeframe}", timeout=15.0)
    res.raise_for_status()
    return res.json()


def stop_strategy(client: httpx.Client, bot_id: int, timeframe: str) -> dict:
    res = client.post(
        "/api/strategy/stop",
        json={"bot_id": bot_id, "timeframe": timeframe},
        timeout=30.0,
    )
    if res.status_code != 200:
        raise RuntimeError(f"stop failed: {res.status_code} {res.text}")
    body = res.json()
    _print("STOP", str(body.get("status")))
    return body


def poll_status(
    client: httpx.Client,
    bot_id: int,
    timeframe: str,
    *,
    max_seconds: int = 90,
    interval: int = 5,
) -> dict:
    """Poll /status until runner_alive becomes True OR timeout. Returns last seen body."""
    deadline = time.monotonic() + max_seconds
    last_body: dict[str, Any] = {}
    saw_alive = False
    saw_signal = False
    while time.monotonic() < deadline:
        try:
            body = fetch_status(client, bot_id, timeframe)
        except httpx.HTTPError as exc:
            _print("POLL", f"http err {exc}")
            time.sleep(interval)
            continue
        last_body = body
        st = body.get("state", {})
        alive = body.get("runner_alive")
        sig_at = st.get("last_signal_at")
        tick_at = st.get("last_tick_at")
        _print(
            "POLL",
            f"alive={alive} is_running={st.get('is_running')} "
            f"last_tick={tick_at} last_signal={sig_at} err={st.get('last_error')}",
        )
        if alive:
            saw_alive = True
        if sig_at:
            saw_signal = True
        if saw_alive and saw_signal:
            return last_body
        time.sleep(interval)
    last_body["_poll_summary"] = {
        "saw_alive": saw_alive,
        "saw_signal": saw_signal,
    }
    return last_body


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--prod", action="store_true", help="hit production instead of localhost")
    parser.add_argument("--bot-id", type=int, default=int(os.environ.get("E2E_BOT_ID", "5")))
    parser.add_argument("--timeframe", default=os.environ.get("E2E_TIMEFRAME", "1m"))
    parser.add_argument(
        "--symbols",
        default=os.environ.get("E2E_SYMBOLS", "BTCUSD"),
        help="CSV list of symbols",
    )
    parser.add_argument(
        "--max-poll",
        type=int,
        default=90,
        help="Max seconds to poll for runner_alive",
    )
    parser.add_argument(
        "--skip-login",
        action="store_true",
        help="Skip login (assumes you've set TC_SESSION env var instead)",
    )
    args = parser.parse_args()

    base = PROD_BASE if args.prod else LOCAL_BASE
    email = os.environ.get("E2E_LOGIN_EMAIL", "butler135@gmail.com")
    symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
    user_emails = [email]

    print(f"\n{'=' * 70}")
    print(f"  Quant Runner E2E test")
    print(f"  Base: {base}")
    print(f"  Bot:  {args.bot_id}  TF: {args.timeframe}  Symbols: {symbols}")
    print(f"  User: {email}")
    print(f"{'=' * 70}\n")

    summary: dict[str, Any] = {
        "base": base,
        "bot_id": args.bot_id,
        "timeframe": args.timeframe,
        "symbols": symbols,
        "started_at": _now_iso(),
    }

    with httpx.Client(base_url=base, follow_redirects=True) as client:
        # If skip-login provided + TC_SESSION env, attach cookie
        if args.skip_login:
            tok = os.environ.get("TC_SESSION", "")
            if tok:
                client.cookies.set("tc_session", tok)
                _print("LOGIN", "using TC_SESSION cookie from env")
        else:
            login(client, email)

        # 1. START
        try:
            start_body = start_strategy(
                client, args.bot_id, args.timeframe, symbols, user_emails
            )
            summary["start_response"] = start_body
        except Exception as exc:
            summary["error_at_start"] = str(exc)
            print(json.dumps(summary, indent=2, default=str))
            return 2

        # 2. POLL
        last_body = poll_status(
            client, args.bot_id, args.timeframe, max_seconds=args.max_poll, interval=5
        )
        summary["status_after_poll"] = {
            "runner_alive": last_body.get("runner_alive"),
            "is_running": last_body.get("state", {}).get("is_running"),
            "last_tick_at": last_body.get("state", {}).get("last_tick_at"),
            "last_signal_at": last_body.get("state", {}).get("last_signal_at"),
            "last_error": last_body.get("state", {}).get("last_error"),
            "recent_trade_count": len(last_body.get("recent_trades", [])),
        }

        # 3. STOP
        try:
            stop_body = stop_strategy(client, args.bot_id, args.timeframe)
            summary["stop_response"] = stop_body
        except Exception as exc:
            summary["error_at_stop"] = str(exc)

        # 4. Confirm not alive
        time.sleep(2)
        try:
            after_stop = fetch_status(client, args.bot_id, args.timeframe)
            summary["status_after_stop"] = {
                "runner_alive": after_stop.get("runner_alive"),
                "is_running": after_stop.get("state", {}).get("is_running"),
            }
        except Exception as exc:
            summary["error_at_final_status"] = str(exc)

    summary["finished_at"] = _now_iso()

    print("\n" + "=" * 70)
    print("  STRUCTURED SUMMARY")
    print("=" * 70)
    print(json.dumps(summary, indent=2, default=str))

    # Exit non-zero if runner never went alive
    poll = summary.get("status_after_poll", {})
    if not poll.get("runner_alive") and not poll.get("is_running"):
        print("\nFAIL: runner never went alive within poll budget")
        return 3
    print("\nOK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
