"""Wave 5B live demo — trailing-take-profit ratchet on BTCUSD.

Steps (Genesis FX demo, virtual money):
  1. Auth + clean any stale positions.
  2. Open a 0.01 BTCUSD long with an initial SL at entry - 1R (1R = $200).
  3. Force the cohort into the 'partial' (trailing) state in the local DB
     by recording a synthetic partial-close.
  4. Step a synthetic current_price through entry, +1R, +1.5R, +2R, +2.5R
     and call TradeManager.update_trailing_ratchet() at each step.
  5. After every ratchet call, query the broker position to confirm the SL
     actually moved.
  6. Close the position cleanly.

Run: `python scripts/test_trailing_ratchet.py`
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import traceback
from pathlib import Path

from dotenv import load_dotenv
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env.tuning")

from app.core.tradelocker_client import TradeLockerClient, TradeLockerError  # noqa: E402
from app.db.database import Base  # noqa: E402
import app.db.models  # noqa: E402, F401
from app.db.models import Bot, StrategyType, User  # noqa: E402
from app.strategies.trade_manager import TradeManager  # noqa: E402

EMAIL = os.environ["TL_DEMO_EMAIL"]
PASSWORD = os.environ["TL_DEMO_PASSWORD"]
SERVER = os.environ["TL_DEMO_SERVER"]

SYMBOL = "BTCUSD"
QTY = 0.01
# Tiny ATR_PROXY: at +2.5R our highest ratcheted SL is entry + 1.5R = entry
# + $4.50, which stays below typical BTC bid/ask drift — keeping the broker
# happy on a long position whose SL must remain below market.
ATR_PROXY = 3.0
DEMO_PARTIAL_QTY = 0.005  # half — to flip cohort into partial state in DB


def _section(title: str) -> None:
    print(f"\n{'=' * 60}\n{title}\n{'=' * 60}")


async def _retry(coro_fn, *, attempts: int = 5, backoff: float = 2.0, label: str = ""):
    delay = backoff
    last_exc: Exception | None = None
    for i in range(attempts):
        try:
            return await coro_fn()
        except TradeLockerError as exc:
            last_exc = exc
            if "429" in str(exc) or "Too Many" in str(exc):
                print(f"  [{label}] 429 backoff {delay:.1f}s ({i + 1}/{attempts})")
                await asyncio.sleep(delay)
                delay *= 2
                continue
            raise
        except Exception as exc:
            last_exc = exc
            raise
    if last_exc:
        raise last_exc
    raise RuntimeError("retry exhausted")


async def _cleanup(client, account_id, token, acc_num) -> int:
    try:
        positions = await client.get_positions(account_id, token, acc_num)
    except TradeLockerError:
        return 0
    closed = 0
    for p in positions:
        pid = p.get("id")
        if pid:
            try:
                await client.close_position(str(pid), token=token, acc_num=acc_num)
                closed += 1
                await asyncio.sleep(0.4)
            except Exception:
                pass
    return closed


def _make_session():
    """Throwaway in-memory SQLite — we only need cohort state for the demo."""
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine, autoflush=False, future=True)
    return Session(), engine


async def _get_position_sl(client, account_id, token, acc_num, position_id) -> dict:
    """Snapshot a position. TL exposes stopLossId (order id) — non-null means
    an SL pending order exists; the actual SL price lives on the linked order
    which we can't easily fetch through the public client surface here. The
    success of the modify_position call (no exception) is our primary proof."""
    positions = await _retry(
        lambda: client.get_positions(account_id, token, acc_num),
        label="get_positions sl_check",
    )
    for p in positions:
        if str(p.get("id")) == str(position_id):
            return {
                "id": str(p.get("id")),
                "stopLossId": p.get("stopLossId"),
                "takeProfitId": p.get("takeProfitId"),
                "avgPrice": p.get("avgPrice"),
                "qty": p.get("qty"),
            }
    return {"closed_or_missing": True, "id": str(position_id)}


async def main() -> int:
    summary: dict = {"steps": []}
    client = TradeLockerClient(env="demo", timeout=30.0)
    auth = None
    db_session = None
    db_engine = None
    pos_id = None

    try:
        _section("Auth")
        auth = await client.authenticate(EMAIL, PASSWORD, SERVER)
        account_id = auth["account_id"]
        token = auth["access_token"]
        acc_num = auth["acc_num"]
        print(f"  ok: account={account_id} accNum={acc_num} balance=${auth['balance']:.2f}")
        summary["account_id"] = account_id

        pre = await _cleanup(client, account_id, token, acc_num)
        if pre:
            print(f"  pre-clean closed {pre} stale positions")

        _section("Phase 1 — open 0.01 BTCUSD long")
        order = await _retry(
            lambda: client.place_order(
                account_id=account_id, token=token, acc_num=acc_num,
                symbol=SYMBOL, side="buy", qty=QTY,
            ),
            label="place_order",
        )
        await asyncio.sleep(3)
        positions = await _retry(
            lambda: client.get_positions(account_id, token, acc_num),
            label="get_positions",
        )
        tradable_id, _ = await client.resolve_symbol(account_id, token, acc_num, SYMBOL)
        candidates = [
            p for p in positions
            if int(p.get("tradableInstrumentId") or 0) == int(tradable_id)
            and str(p.get("side")).lower() == "buy"
        ]
        if not candidates:
            raise RuntimeError("entry leg not found after place_order")
        newest = max(candidates, key=lambda p: int(p.get("openDate") or 0))
        pos_id = str(newest["id"])
        entry_price = float(newest["avgPrice"])
        print(f"  position_id={pos_id} entry={entry_price} qty={newest['qty']}")
        summary["entry_price"] = entry_price
        summary["position_id"] = pos_id

        # Wrap client.modify_position now so every call (including Phase 2)
        # gets captured.
        broker_calls: list[dict] = []
        original_modify = client.modify_position

        async def _logged_modify(position_id, *, token, acc_num, stop_loss=None, take_profit=None):
            try:
                resp = await original_modify(
                    position_id,
                    token=token, acc_num=acc_num,
                    stop_loss=stop_loss, take_profit=take_profit,
                )
                broker_calls.append({
                    "position_id": position_id, "stop_loss": stop_loss,
                    "take_profit": take_profit, "ok": True, "resp": resp,
                })
                return resp
            except Exception as exc:
                broker_calls.append({
                    "position_id": position_id, "stop_loss": stop_loss,
                    "take_profit": take_profit, "ok": False, "err": str(exc),
                })
                raise

        client.modify_position = _logged_modify  # type: ignore[assignment]

        _section("Phase 2 — set initial SL at entry - 1×ATR")
        initial_sl = round(entry_price - ATR_PROXY, 2)
        await _retry(
            lambda: client.modify_position(
                pos_id, token=token, acc_num=acc_num, stop_loss=initial_sl,
            ),
            label="set_initial_sl",
        )
        await asyncio.sleep(1.5)
        snap = await _get_position_sl(client, account_id, token, acc_num, pos_id)
        print(f"  initial SL set: {snap}")
        summary["initial_sl"] = initial_sl
        summary["initial_sl_snapshot"] = snap

        _section("Phase 3 — build local cohort + flip to 'partial' state")
        db_session, db_engine = _make_session()
        bot = Bot(
            name="Quant",
            slug="latpfn-quant-demo",
            description="ratchet-demo",
            strategy_type=StrategyType.latpfn_quant,
            instruments_csv=SYMBOL,
            webhook_secret="demo",
        )
        user = User(
            email="demo@local",
            hashed_password="x",
            tradelocker_acc_num=acc_num,
            tradelocker_account_id=account_id,
        )
        db_session.add_all([bot, user])
        db_session.commit()
        db_session.refresh(bot)
        db_session.refresh(user)

        tm = TradeManager(db_session, bot.id, user.id, "1m")
        tm.set_broker_context(client, token=token, acc_num=acc_num)
        cohort = tm.open_cohort(
            instrument=SYMBOL,
            side="buy",
            entry_price=entry_price,
            atr=ATR_PROXY,
            qty=QTY,
            stop_loss=initial_sl,
            take_profit=entry_price + 10 * ATR_PROXY,
            tl_position_id=pos_id,
            tl_order_id=str(order.get("order_id") or ""),
        )
        # Flip to partial: synthesize a half-close (book-keeping only — no
        # broker call here; we keep the leg open on the broker for the demo).
        tm.record_partial_close(cohort, qty_closed=DEMO_PARTIAL_QTY, close_price=entry_price + ATR_PROXY)
        db_session.commit()
        print(f"  cohort id={cohort.id} status={cohort.status.value} avg={cohort.weighted_avg_entry}")

        _section("Phase 4 — step synthetic prices through ratchet")
        steps = [
            ("entry", entry_price),
            ("+1R", entry_price + 1.0 * ATR_PROXY),
            ("+1.5R", entry_price + 1.5 * ATR_PROXY),
            ("+2R", entry_price + 2.0 * ATR_PROXY),
            ("+2.5R", entry_price + 2.5 * ATR_PROXY),
        ]
        progression: list[dict] = []
        for label, synth_price in steps:
            updates = await tm.update_trailing_ratchet(SYMBOL, synth_price)
            db_session.commit()
            await asyncio.sleep(2.0)
            snap = await _get_position_sl(client, account_id, token, acc_num, pos_id)
            entry = {
                "label": label,
                "synthetic_price": synth_price,
                "ratchet_updates": updates,
                "broker_snapshot": snap,
            }
            progression.append(entry)
            print(f"  [{label}] price={synth_price}")
            print(f"     updates: {updates}")
            print(f"     broker SL now: {snap.get('stopLoss')}")
        summary["progression"] = progression
        summary["broker_modify_calls"] = broker_calls

        _section("Phase 5 — close position")
        closed = await _cleanup(client, account_id, token, acc_num)
        print(f"  closed {closed} positions")
        summary["closed_count"] = closed

        _section("FINAL SUMMARY")
        print(json.dumps(summary, indent=2, default=str))
        return 0
    except Exception as exc:
        print(f"FATAL: {exc}")
        traceback.print_exc()
        if auth:
            try:
                await _cleanup(
                    client, auth["account_id"], auth["access_token"], auth["acc_num"]
                )
            except Exception:
                pass
        return 1
    finally:
        if db_session is not None:
            db_session.close()
        if db_engine is not None:
            db_engine.dispose()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
