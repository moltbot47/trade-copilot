"""End-to-end lifecycle test for the LaT-PFN Quant Trader on Genesis FX demo.

Runs the full management state machine on BTCUSD + ETHUSD using real broker
calls (demo only, virtual money). Sequence per symbol:

  1. Open initial cohort (synthetic +momentum signal — just buy)
  2. Wait 30s, then trigger scale-in helper
  3. Wait 30s, then trigger partial-close helper
  4. Wait 30s, then trigger SL move (trail) helper
  5. Wait 30s, then close all legs

Cleans up on exit (closes all positions even on error).

Run: `python scripts/test_quant_lifecycle.py`
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import traceback
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env.tuning")

from app.core.tradelocker_client import TradeLockerClient, TradeLockerError  # noqa: E402

EMAIL = os.environ["TL_DEMO_EMAIL"]
PASSWORD = os.environ["TL_DEMO_PASSWORD"]
SERVER = os.environ["TL_DEMO_SERVER"]


SYMBOLS = ["BTCUSD", "ETHUSD"]
QTY_PER_LEG = 0.01  # min lot for crypto
ATR_PROXY_PCT = 0.005  # use ±0.5% as a proxy for "1×ATR" in this demo run
SLEEP_BETWEEN_PHASES = 8  # seconds (kept short to stay below TL rate-limit budget)


def _print_section(title: str) -> None:
    print(f"\n{'=' * 60}\n{title}\n{'=' * 60}")


async def _get_current_price(client, account_id, token, acc_num, symbol) -> float:
    """Use the avg price from a freshly-opened tiny position is too costly,
    so we rely on the position's avgPrice after we open the entry leg."""
    raise NotImplementedError  # not used — we lift the price from the position record


async def _retry(coro_fn, *, attempts: int = 5, backoff: float = 2.0, label: str = ""):
    """Retry helper that handles 429 rate limits with exponential backoff."""
    delay = backoff
    last_exc: Exception | None = None
    for i in range(attempts):
        try:
            return await coro_fn()
        except TradeLockerError as exc:
            last_exc = exc
            msg = str(exc)
            if "429" in msg or "Too Many" in msg:
                print(f"  [{label}] 429 backoff {delay:.1f}s (attempt {i + 1}/{attempts})")
                await asyncio.sleep(delay)
                delay *= 2
                continue
            raise
        except Exception as exc:
            last_exc = exc
            raise
    if last_exc:
        raise last_exc
    raise RuntimeError("retry exhausted with no exception")


async def open_leg(
    client: TradeLockerClient,
    account_id: str,
    token: str,
    acc_num: str,
    symbol: str,
    side: str,
    qty: float,
    sl: float | None = None,
    tp: float | None = None,
) -> dict:
    """Open a leg, return {position_id, fill_price}."""
    order = await _retry(
        lambda: client.place_order(
            account_id=account_id, token=token, acc_num=acc_num,
            symbol=symbol, side=side, qty=qty, sl=sl, tp=tp,
        ),
        label=f"place_order {symbol}",
    )
    await asyncio.sleep(3)
    positions = await _retry(
        lambda: client.get_positions(account_id, token, acc_num),
        label="get_positions",
    )
    # newest position with matching symbol+side
    tradable_id, _ = await client.resolve_symbol(account_id, token, acc_num, symbol)
    candidates = [
        p for p in positions
        if int(p.get("tradableInstrumentId") or 0) == int(tradable_id)
        and str(p.get("side")).lower() == side.lower()
    ]
    if not candidates:
        raise RuntimeError(f"could not locate freshly-opened {side} on {symbol}")
    newest = max(candidates, key=lambda p: int(p.get("openDate") or 0))
    return {
        "position_id": str(newest["id"]),
        "fill_price": float(newest["avgPrice"]),
        "qty": float(newest["qty"]),
        "order_id": str(order.get("order_id") or ""),
    }


async def cleanup_all_positions(client, account_id, token, acc_num) -> int:
    """DELETE every open position. Returns count closed."""
    try:
        positions = await _retry(
            lambda: client.get_positions(account_id, token, acc_num),
            label="cleanup get_positions",
            attempts=3,
        )
    except TradeLockerError:
        return 0
    closed = 0
    for p in positions:
        pid = p.get("id")
        if pid:
            try:
                await _retry(
                    lambda pid=pid: client.close_position(str(pid), token=token, acc_num=acc_num),
                    label=f"close {pid}",
                    attempts=3,
                )
                closed += 1
                await asyncio.sleep(0.4)
            except Exception:
                pass
    return closed


async def lifecycle_for_symbol(
    client: TradeLockerClient,
    account_id: str,
    token: str,
    acc_num: str,
    symbol: str,
) -> dict:
    """Run the full 5-phase lifecycle for one symbol. Returns summary dict."""
    summary: dict = {"symbol": symbol, "phases": [], "legs": []}

    _print_section(f"{symbol} — Phase 1: open initial cohort")
    leg1 = await open_leg(client, account_id, token, acc_num, symbol, "buy", QTY_PER_LEG)
    summary["legs"].append({"role": "entry", **leg1})
    summary["phases"].append({
        "phase": "open_entry",
        "ts": datetime.utcnow().isoformat(),
        "fill_price": leg1["fill_price"],
        "qty": leg1["qty"],
    })
    print(f"  entry leg: position_id={leg1['position_id']} fill={leg1['fill_price']} qty={leg1['qty']}")
    initial_avg = leg1["fill_price"]

    print(f"  sleeping {SLEEP_BETWEEN_PHASES}s ...")
    await asyncio.sleep(SLEEP_BETWEEN_PHASES)

    _print_section(f"{symbol} — Phase 2: scale-in (additional leg)")
    leg2 = await open_leg(client, account_id, token, acc_num, symbol, "buy", QTY_PER_LEG)
    summary["legs"].append({"role": "scale_in", **leg2})
    weighted_avg = (leg1["fill_price"] + leg2["fill_price"]) / 2.0
    summary["phases"].append({
        "phase": "scale_in",
        "ts": datetime.utcnow().isoformat(),
        "fill_price": leg2["fill_price"],
        "qty": leg2["qty"],
        "new_weighted_avg_entry": weighted_avg,
    })
    print(f"  scale-in: position_id={leg2['position_id']} fill={leg2['fill_price']} → avg={weighted_avg:.4f}")

    # Move entry leg's SL to break-even
    breakeven_sl = round(initial_avg * 0.999, 4)  # very near entry, below current price
    try:
        await _retry(
            lambda: client.modify_position(
                leg1["position_id"], token=token, acc_num=acc_num,
                stop_loss=breakeven_sl,
            ),
            label="modify_sl_breakeven",
        )
        summary["phases"].append({
            "phase": "scale_in_move_entry_sl_to_breakeven",
            "leg": leg1["position_id"],
            "new_sl": breakeven_sl,
        })
        print(f"  moved entry leg SL to {breakeven_sl}")
    except Exception as exc:
        print(f"  WARN: SL move failed: {exc}")
        summary["phases"].append({"phase": "modify_sl_failed", "error": str(exc)})

    print(f"  sleeping {SLEEP_BETWEEN_PHASES}s ...")
    await asyncio.sleep(SLEEP_BETWEEN_PHASES)

    _print_section(f"{symbol} — Phase 3: scale-out (partial close 50%)")
    # Open opposite-side hedge order to net out half
    hedge = await _retry(
        lambda: client.partial_close(
            account_id=account_id, token=token, acc_num=acc_num,
            position_id=leg1["position_id"],
            symbol=symbol, original_side="buy",
            qty=QTY_PER_LEG,
        ),
        label="partial_close",
    )
    summary["phases"].append({
        "phase": "partial_close",
        "ts": datetime.utcnow().isoformat(),
        "qty_closed": QTY_PER_LEG,
        "hedge_order_id": hedge.get("order_id"),
    })
    print(f"  partial-close hedge order={hedge.get('order_id')}")
    summary["legs"].append({"role": "hedge_close", "order_id": hedge.get("order_id")})

    print(f"  sleeping {SLEEP_BETWEEN_PHASES}s ...")
    await asyncio.sleep(SLEEP_BETWEEN_PHASES)

    _print_section(f"{symbol} — Phase 4: trail SL")
    # Pull current price to compute new trailing SL
    positions = await _retry(
        lambda: client.get_positions(account_id, token, acc_num),
        label="get_positions trail",
    )
    new_sl_for_remaining = None
    for leg_pos_id in [leg1["position_id"], leg2["position_id"]]:
        match = next((p for p in positions if str(p.get("id")) == leg_pos_id), None)
        if match is None:
            continue
        cur_price = float(match["avgPrice"])  # hedged demo doesn't expose mark, so we use avg as proxy
        new_sl = round(cur_price * (1 - ATR_PROXY_PCT * 0.5), 4)
        try:
            await _retry(
                lambda lid=leg_pos_id, ns=new_sl: client.modify_position(lid, token=token, acc_num=acc_num, stop_loss=ns),
                label=f"trail {leg_pos_id}",
            )
            summary["phases"].append({
                "phase": "trail_sl",
                "leg": leg_pos_id,
                "new_sl": new_sl,
            })
            new_sl_for_remaining = new_sl
            print(f"  trailed SL on {leg_pos_id} → {new_sl}")
        except Exception as exc:
            print(f"  WARN trail SL failed: {exc}")
            summary["phases"].append({"phase": "trail_sl_failed", "leg": leg_pos_id, "error": str(exc)})

    print(f"  sleeping {SLEEP_BETWEEN_PHASES}s ...")
    await asyncio.sleep(SLEEP_BETWEEN_PHASES)

    _print_section(f"{symbol} — Phase 5: full close")
    closed = await cleanup_all_positions(client, account_id, token, acc_num)
    summary["phases"].append({
        "phase": "full_close",
        "ts": datetime.utcnow().isoformat(),
        "closed_count": closed,
    })
    print(f"  closed {closed} positions (whole account)")

    # Get final account state for net P&L
    state = await client.get_account_state(account_id, token, acc_num)
    summary["final_balance"] = state.get("balance")
    summary["final_today_net"] = state.get("todayNet")
    summary["final_today_gross"] = state.get("todayGross")
    return summary


async def main() -> int:
    overall = {"started_at": datetime.utcnow().isoformat(), "results": []}
    client = TradeLockerClient(env="demo", timeout=30.0)
    auth = None
    try:
        _print_section("Auth")
        auth = await client.authenticate(EMAIL, PASSWORD, SERVER)
        account_id = auth["account_id"]
        token = auth["access_token"]
        acc_num = auth["acc_num"]
        print(f"  ok: account={account_id} accNum={acc_num} balance=${auth['balance']:.2f}")

        # Pre-clean
        pre = await cleanup_all_positions(client, account_id, token, acc_num)
        if pre:
            print(f"  pre-clean: closed {pre} stale positions")

        opening_state = await client.get_account_state(account_id, token, acc_num)
        overall["opening_balance"] = opening_state.get("balance")

        for symbol in SYMBOLS:
            try:
                summary = await lifecycle_for_symbol(client, account_id, token, acc_num, symbol)
                overall["results"].append(summary)
            except Exception as exc:
                tb = traceback.format_exc()
                print(f"\nERROR on {symbol}: {exc}\n{tb}")
                overall["results"].append({"symbol": symbol, "error": str(exc), "traceback": tb})
                # ensure we cleanup before next symbol
                await cleanup_all_positions(client, account_id, token, acc_num)

        # Final cleanup + state
        final_clean = await cleanup_all_positions(client, account_id, token, acc_num)
        if final_clean:
            print(f"\nfinal cleanup closed {final_clean} positions")
        await asyncio.sleep(5)
        final_state = await _retry(
            lambda: client.get_account_state(account_id, token, acc_num),
            label="final state",
            attempts=6,
        )
        positions_left = await _retry(
            lambda: client.get_positions(account_id, token, acc_num),
            label="final positions",
            attempts=6,
        )
        overall["positions_left_open"] = len(positions_left)
        overall["closing_balance"] = final_state.get("balance")
        overall["today_net_pnl"] = final_state.get("todayNet")
        overall["today_gross_pnl"] = final_state.get("todayGross")

        _print_section("FINAL SUMMARY")
        print(json.dumps(overall, indent=2, default=str))
        if overall["positions_left_open"] != 0:
            print("\nWARN: positions still open!")
            return 2
        return 0
    except Exception as exc:
        print(f"FATAL: {exc}")
        traceback.print_exc()
        if auth:
            try:
                await cleanup_all_positions(
                    client, auth["account_id"], auth["access_token"], auth["acc_num"]
                )
            except Exception:
                pass
        return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
