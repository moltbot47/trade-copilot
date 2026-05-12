"""Bot health check — read-only diagnostic across every bot + subscribed user.

NO TRADES ARE PLACED. NO POSITIONS MODIFIED. Pure read paths only:
  - DB queries (bots, strategy_state, subscriptions, cohorts)
  - TradeLocker: get_account_state, get_positions, get_instruments (no order calls)
  - BarFetcher: fetch_bars (read-only HTTP)
  - LaT-PFN: HTTP HEAD against the configured endpoint (no forecast call)

Exit code is 0 if every check passes, 1 if any FAIL was emitted, 2 on
crash. WARN-level findings do not change the exit code.

Run locally (using prod DB via tunnel) or, more usually, inside the Fly
container:

    fly ssh console -C "python /app/scripts/bot_health_check.py" -a trade-copilot-api

The output is a series of [PASS]/[WARN]/[FAIL] lines grouped by bot and
user. Designed to be cron-able + diff-able across runs.
"""
from __future__ import annotations

import asyncio
import os
import sys
import traceback
from datetime import datetime, timezone
from typing import Any

# Allow running as `python scripts/bot_health_check.py` from the backend root
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import select  # noqa: E402

from app.core.crypto import decrypt  # noqa: E402
from app.core.tradelocker_client import TradeLockerClient  # noqa: E402
from app.db.database import SessionLocal  # noqa: E402
from app.db.models import (  # noqa: E402
    Bot,
    Cohort,
    CohortStatus,
    StrategyState,
    Subscription,
    User,
)
from app.strategies.data_feed import BarFetcher  # noqa: E402
from app.strategies.tiny_account_advisor import _MARGIN_AT_MIN_LOT_USD  # noqa: E402


_FAIL_COUNT = 0
_WARN_COUNT = 0


def _emit(level: str, msg: str) -> None:
    global _FAIL_COUNT, _WARN_COUNT
    print(f"[{level}] {msg}")
    if level == "FAIL":
        _FAIL_COUNT += 1
    elif level == "WARN":
        _WARN_COUNT += 1


def _section(title: str) -> None:
    print()
    print("=" * 72)
    print(title)
    print("=" * 72)


def _now_utc() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


# -------------- per-bot checks (no broker calls) --------------
def check_bot_configuration(db) -> list[Bot]:
    """Returns the active bots so callers can iterate them."""
    _section("Bot catalog")
    bots = db.scalars(select(Bot)).all()
    if not bots:
        _emit("FAIL", "No bots defined in DB — frontend would show empty catalog.")
        return []

    margin_table_symbols = set(_MARGIN_AT_MIN_LOT_USD.keys())
    for bot in bots:
        instruments = [
            s.strip().upper() for s in (bot.instruments_csv or "").split(",") if s.strip()
        ]
        if not instruments:
            _emit("FAIL", f"{bot.slug}: instruments_csv is empty — bot can't trade anything.")
            continue
        unknown = [s for s in instruments if s not in margin_table_symbols]
        if unknown:
            _emit(
                "WARN",
                f"{bot.slug}: {len(unknown)} instrument(s) not in margin table "
                f"({', '.join(unknown)}) — pre-flight margin check will skip them.",
            )
        _emit(
            "PASS",
            f"{bot.slug:<24} active={bot.is_active} risk={bot.risk_level} "
            f"instruments={','.join(instruments)}",
        )
    return bots


def check_strategy_states(db, bots: list[Bot]) -> None:
    _section("Strategy state per (bot, timeframe)")
    states = db.scalars(select(StrategyState)).all()
    if not states:
        _emit("WARN", "No strategy_state rows — no bots have ever been started.")
        return

    bot_by_id = {b.id: b for b in bots}
    now = _now_utc()
    for st in states:
        bot = bot_by_id.get(st.bot_id)
        label = f"{bot.slug if bot else f'bot#{st.bot_id}'} @ {st.timeframe}"

        # Sanity bands on threshold + concurrency cap.
        if st.confidence_threshold < 0.1 or st.confidence_threshold > 4.0:
            _emit(
                "FAIL",
                f"{label}: confidence_threshold={st.confidence_threshold:.2f} "
                f"outside sane band [0.1, 4.0]",
            )
        if st.max_concurrent < 1 or st.max_concurrent > 25:
            _emit(
                "WARN",
                f"{label}: max_concurrent={st.max_concurrent} outside expected band [1, 25]",
            )

        # Running but stale tick = silently wedged.
        if st.is_running and st.last_tick_at is not None:
            age_s = (now - st.last_tick_at).total_seconds()
            if age_s > 600:
                _emit(
                    "FAIL",
                    f"{label}: is_running=True but last_tick {int(age_s)}s ago "
                    f"— runner may be wedged.",
                )
            elif age_s > 120:
                _emit(
                    "WARN",
                    f"{label}: last_tick {int(age_s)}s ago — slow or under load.",
                )
            else:
                _emit(
                    "PASS",
                    f"{label}: running, last_tick {int(age_s)}s ago, "
                    f"threshold={st.confidence_threshold:.2f}σ, "
                    f"max_concurrent={st.max_concurrent}",
                )
        elif st.is_running:
            _emit("WARN", f"{label}: is_running=True but last_tick_at is NULL.")
        else:
            _emit("PASS", f"{label}: stopped (clean).")

        if st.last_error:
            # Truncate so the report stays scannable.
            snippet = st.last_error[:120].replace("\n", " ")
            _emit("WARN", f"{label}: last_error recorded — {snippet}")


# -------------- broker-truth checks (read-only) --------------
async def check_user_broker(db, user: User, client: TradeLockerClient) -> None:
    """Per-user read-only broker probe. Skips users with no creds."""
    label = f"user#{user.id} ({user.email})"
    if not user.tradelocker_token or not user.tradelocker_account_id:
        _emit("PASS", f"{label}: no broker linked — skipped.")
        return

    token = decrypt(user.tradelocker_token)
    acc_id = user.tradelocker_account_id
    acc_num = user.tradelocker_acc_num or ""
    if not token:
        _emit("FAIL", f"{label}: tradelocker_token failed to decrypt.")
        return

    # Account state read
    try:
        state = await client.get_account_state(acc_id, token, acc_num)
    except Exception as exc:
        _emit("FAIL", f"{label}: account state read failed — {exc.__class__.__name__}: {exc}")
        return

    balance = state.get("balance") or state.get("netAssetValue")
    available = state.get("availableFunds")
    if balance is None:
        _emit("WARN", f"{label}: account state returned no balance field.")
    else:
        _emit(
            "PASS",
            f"{label}: balance=${float(balance):.2f}, available=${float(available or 0):.2f}, "
            f"env={user.tradelocker_env}",
        )

    # Open positions: check each has SL + TP attached (broker truth).
    try:
        positions = await client.get_positions(acc_id, token, acc_num)
    except Exception as exc:
        _emit("FAIL", f"{label}: get_positions failed — {exc.__class__.__name__}: {exc}")
        return

    if not positions:
        _emit("PASS", f"{label}: 0 open broker positions.")
    else:
        unprotected = []
        for p in positions:
            sl_id = p.get("stopLossId") or p.get("stop_loss_id")
            tp_id = p.get("takeProfitId") or p.get("take_profit_id")
            if not sl_id or not tp_id:
                unprotected.append(p)
        if unprotected:
            _emit(
                "FAIL",
                f"{label}: {len(unprotected)}/{len(positions)} broker position(s) "
                f"missing SL or TP — reconciliation should auto-protect these.",
            )
            for p in unprotected:
                pid = p.get("id") or p.get("positionId") or "?"
                sym = p.get("symbol") or p.get("instrumentName") or "?"
                _emit(
                    "FAIL",
                    f"  ↳ position {pid} on {sym}: stopLossId={p.get('stopLossId')}, "
                    f"takeProfitId={p.get('takeProfitId')}",
                )
        else:
            _emit("PASS", f"{label}: all {len(positions)} broker position(s) protected (SL+TP).")

    # Cross-reference DB cohorts with broker positions
    open_cohorts = db.scalars(
        select(Cohort).where(
            Cohort.user_id == user.id, Cohort.status == CohortStatus.open
        )
    ).all()
    broker_pos_ids = {str(p.get("id")) for p in positions}
    orphan_cohorts = []
    for c in open_cohorts:
        leg_ids = {leg.tradelocker_position_id for leg in c.legs if leg.is_open}
        if leg_ids and leg_ids.isdisjoint(broker_pos_ids):
            orphan_cohorts.append(c)
    if orphan_cohorts:
        _emit(
            "WARN",
            f"{label}: {len(orphan_cohorts)} open cohort(s) reference position IDs "
            f"that don't exist on the broker — likely closed externally.",
        )
        for c in orphan_cohorts:
            _emit("WARN", f"  ↳ cohort #{c.id} {c.side} {c.instrument} opened {c.opened_at}")

    # The inverse: broker positions with no matching cohort leg = pre-existing
    # manual trades or runner crashed mid-open.
    db_leg_ids = {
        leg.tradelocker_position_id
        for c in open_cohorts
        for leg in c.legs
        if leg.is_open and leg.tradelocker_position_id
    }
    orphan_broker = [p for p in positions if str(p.get("id")) not in db_leg_ids]
    if orphan_broker:
        _emit(
            "WARN",
            f"{label}: {len(orphan_broker)} broker position(s) not tracked by any "
            f"cohort — manual trade or unmanaged.",
        )


# -------------- data feed (bars) probe --------------
async def check_bar_fetch(user: User, client: TradeLockerClient, symbols: list[str]) -> None:
    label = f"user#{user.id} bar feed"
    if not user.tradelocker_token:
        return
    token = decrypt(user.tradelocker_token)
    if not token:
        return
    fetcher = BarFetcher(
        client=client,
        account_id=user.tradelocker_account_id,
        token=token,
        acc_num=user.tradelocker_acc_num or "",
    )
    for symbol in symbols:
        try:
            df = await fetcher.fetch_bars(symbol, timeframe="1m", count=60)
        except Exception as exc:
            _emit("FAIL", f"{label}: {symbol} fetch raised {exc.__class__.__name__}: {exc}")
            continue
        if df is None or len(df) == 0:
            _emit("FAIL", f"{label}: {symbol} returned empty bars.")
            continue
        if df.attrs.get("synthetic"):
            _emit(
                "FAIL",
                f"{label}: {symbol} bars flagged synthetic — broker feed degraded.",
            )
            continue
        _emit(
            "PASS",
            f"{label}: {symbol} {len(df)} real bars, last close {float(df['close'].iloc[-1]):.4f}",
        )


# -------------- LaT-PFN service probe --------------
async def check_latpfn() -> None:
    _section("LaT-PFN inference service")
    url = (os.getenv("LATPFN_ENDPOINT_URL") or "").strip()
    if not url:
        _emit("FAIL", "LATPFN_ENDPOINT_URL env not set — bots will use cached/synthetic.")
        return
    import httpx

    base = url.rsplit("/", 1)[0] if url.endswith("/forecast") else url
    health_url = base.rstrip("/") + "/health"
    try:
        async with httpx.AsyncClient(timeout=5.0) as h:
            r = await h.get(health_url)
        if r.status_code == 200:
            _emit("PASS", f"LaT-PFN /health 200 ({health_url})")
        else:
            _emit("WARN", f"LaT-PFN /health returned {r.status_code} ({health_url})")
    except Exception as exc:
        _emit(
            "FAIL",
            f"LaT-PFN unreachable at {health_url}: {exc.__class__.__name__}: {exc}",
        )


# -------------- orchestrator --------------
async def main() -> int:
    print(f"Trade Copilot bot health check — {_now_utc().isoformat()}Z")
    print("Read-only. No trades will be placed.\n")

    db = SessionLocal()
    try:
        bots = check_bot_configuration(db)
        check_strategy_states(db, bots)

        # Per-user broker probe + bar feed probe.
        # We share one TradeLockerClient since it's stateless (auth lives in args).
        client = TradeLockerClient()
        users = db.scalars(
            select(User).where(User.tradelocker_token.isnot(None))
        ).all()
        _section(f"Broker truth — {len(users)} linked user(s)")
        for user in users:
            await check_user_broker(db, user, client)

        # Sample bar fetch for each bot's first instrument, via the first user
        # who has broker creds (one fetch is enough to verify the data path).
        _section("Data feed (bars)")
        if users:
            probe_user = users[0]
            sample_symbols = sorted({
                (b.instruments_csv or "").split(",")[0].strip().upper()
                for b in bots
                if b.instruments_csv
            })
            sample_symbols = [s for s in sample_symbols if s]
            await check_bar_fetch(probe_user, client, sample_symbols)
        else:
            _emit("WARN", "No users with broker creds — can't probe bar feed.")

        await check_latpfn()

    finally:
        db.close()

    _section("Summary")
    print(f"FAIL: {_FAIL_COUNT}    WARN: {_WARN_COUNT}")
    return 0 if _FAIL_COUNT == 0 else 1


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        sys.exit(130)
    except Exception:
        traceback.print_exc()
        sys.exit(2)
