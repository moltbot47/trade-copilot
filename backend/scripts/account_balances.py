"""Pull live TradeLocker balance for every connected user. Read-only.

Usage (on Fly):
    fly ssh console -C "python /app/scripts/account_balances.py" -a trade-copilot-api

Output: one row per user with email, env, account id, balance, free margin,
positions/orders count, plus any subscription/runner state flags.
"""
from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import select  # noqa: E402

from app.core.crypto import decrypt  # noqa: E402
from app.core.tradelocker_client import TradeLockerClient, TradeLockerError  # noqa: E402
from app.core.tradelocker_token_refresh import refresh_user_token  # noqa: E402
from app.db.database import SessionLocal  # noqa: E402
from app.db.models import StrategyState, Subscription, User  # noqa: E402


async def _fetch_state(client: TradeLockerClient, user: User, token: str) -> dict:
    return await client.get_account_state(
        user.tradelocker_account_id,
        token,
        user.tradelocker_acc_num or "1",
    )


async def pull_one(user: User) -> dict:
    out: dict = {
        "id": user.id,
        "email": user.email,
        "env": user.tradelocker_env,
        "account_id": user.tradelocker_account_id,
        "acc_num": user.tradelocker_acc_num,
        "bot_paused": user.bot_paused,
    }
    if not user.tradelocker_token or not user.tradelocker_account_id:
        out["error"] = "no token / account"
        return out
    try:
        token = decrypt(user.tradelocker_token)
        client = TradeLockerClient(env=user.tradelocker_env or "demo")
        try:
            state = await _fetch_state(client, user, token)
        except TradeLockerError as exc:
            if "401" not in str(exc) and "unauthorized" not in str(exc).lower():
                raise
            # Refresh and retry once
            new_token = await refresh_user_token(user.id)
            if not new_token:
                out["error"] = "token expired, refresh failed (needs UI re-auth)"
                return out
            state = await _fetch_state(client, user, new_token)
        out.update(
            balance=state.get("balance"),
            available=state.get("availableFunds"),
            blocked=state.get("blockedBalance"),
            init_margin=state.get("initialMarginReq"),
            open_pnl=state.get("openNetPnL"),
            positions=int(state.get("positionsCount") or 0),
            orders=int(state.get("ordersCount") or 0),
        )
    except TradeLockerError as exc:
        out["error"] = f"TL: {exc}"
        return out
    except Exception as exc:  # noqa: BLE001
        out["error"] = f"{type(exc).__name__}: {exc}"
    return out


async def main() -> None:
    with SessionLocal() as db:
        users = db.scalars(select(User).order_by(User.id)).all()
        sub_counts: dict[int, tuple[int, int]] = {}
        for uid, in db.execute(select(Subscription.user_id)).all():
            active, paused = sub_counts.get(uid, (0, 0))
            # second pass below counts paused; this just records presence
            sub_counts[uid] = (active, paused)
        # accurate counts
        sub_counts = {}
        for sub in db.scalars(select(Subscription)).all():
            a, p = sub_counts.get(sub.user_id, (0, 0))
            if sub.is_paused:
                sub_counts[sub.user_id] = (a, p + 1)
            else:
                sub_counts[sub.user_id] = (a + 1, p)
        # StrategyState is keyed by (bot_id, timeframe), NOT user — there's
        # no `user_id` column. To get a per-user runner count we join through
        # Subscription: every active subscriber of a (bot_id) whose state is
        # `is_running` counts as one running runner for that user. Skip
        # paused subscriptions because the runner won't fire for them even
        # if the runtime state is `is_running`.
        running_bot_ids = {
            s.bot_id
            for s in db.scalars(select(StrategyState)).all()
            if s.is_running
        }
        runner_counts: dict[int, int] = {}
        if running_bot_ids:
            for sub in db.scalars(select(Subscription)).all():
                if sub.is_paused or sub.bot_id not in running_bot_ids:
                    continue
                runner_counts[sub.user_id] = runner_counts.get(sub.user_id, 0) + 1

    rows = []
    for u in users:
        if not u.tradelocker_account_id:
            continue
        row = await pull_one(u)
        active, paused = sub_counts.get(u.id, (0, 0))
        row["subs_active"] = active
        row["subs_paused"] = paused
        row["runners_running"] = runner_counts.get(u.id, 0)
        rows.append(row)

    if not rows:
        print("No users with connected TradeLocker accounts.")
        return

    hdr = (
        f"{'id':>3} {'email':<32} {'env':<5} {'acct':<10} {'balance':>10} "
        f"{'free':>10} {'blocked':>10} {'oPnL':>8} {'pos':>3} {'ord':>3} "
        f"{'subA':>4} {'subP':>4} {'run':>3} {'paused':>6}  notes"
    )
    print(hdr)
    print("-" * len(hdr))
    for r in rows:
        if "error" in r:
            print(
                f"{r['id']:>3} {r['email'][:32]:<32} {(r['env'] or '-'):<5} "
                f"{(r['account_id'] or '-'):<10} {'?':>10} {'?':>10} {'?':>10} "
                f"{'?':>8} {'?':>3} {'?':>3} "
                f"{r['subs_active']:>4} {r['subs_paused']:>4} {r['runners_running']:>3} "
                f"{('yes' if r['bot_paused'] else 'no'):>6}  ERR: {r['error']}"
            )
            continue
        print(
            f"{r['id']:>3} {r['email'][:32]:<32} {(r['env'] or '-'):<5} "
            f"{(r['account_id'] or '-'):<10} {r['balance']:>10.2f} "
            f"{r['available']:>10.2f} {r['blocked']:>10.2f} "
            f"{r['open_pnl']:>8.2f} {r['positions']:>3} {r['orders']:>3} "
            f"{r['subs_active']:>4} {r['subs_paused']:>4} {r['runners_running']:>3} "
            f"{('yes' if r['bot_paused'] else 'no'):>6}"
        )


if __name__ == "__main__":
    asyncio.run(main())
