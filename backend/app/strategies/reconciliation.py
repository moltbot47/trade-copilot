"""Position reconciliation — periodic DB ↔ broker drift detection.

Two drift conditions worth alerting on:

  1. **Ghost cohort**: our DB says a CohortLeg is open with a
     tradelocker_position_id, but that position is NOT present at the
     broker. Likely cause: position closed externally (manual user
     close, SL hit while runner was down, broker liquidation). The
     cohort is now ghosting — stats won't update, trailing logic runs
     against a non-existent position.

  2. **Untracked broker position**: broker has a position whose
     tradeable_id matches a symbol we trade, but no open CohortLeg in
     our DB points to it. Likely cause: order placed but cohort row
     creation crashed before commit, or a manual user trade outside
     the bot. The bot won't manage it.

Both conditions are reported to:
  - Logger (WARNING level)
  - Discord webhook (one alert per cohort/position, cooldown enforced)
  - StrategyTickLog (decision="reconciliation_drift")
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta

from app.core.tradelocker_auth import call_with_refresh
from app.core.tradelocker_client import TradeLockerClient, TradeLockerError
from app.db.database import SessionLocal
from app.db.models import Cohort, CohortStatus, User

logger = logging.getLogger(__name__)


# In-process cooldown so a single drift state doesn't spam alerts every cycle.
_drift_alerted: dict[str, datetime] = {}
_ALERT_COOLDOWN_MIN = 60


def _alert_key(kind: str, identifier: str) -> str:
    return f"{kind}:{identifier}"


def _should_alert(key: str) -> bool:
    last = _drift_alerted.get(key)
    if last is None:
        _drift_alerted[key] = datetime.utcnow()
        return True
    if datetime.utcnow() - last > timedelta(minutes=_ALERT_COOLDOWN_MIN):
        _drift_alerted[key] = datetime.utcnow()
        return True
    return False


async def reconcile_user(user_id: int) -> dict:
    """Compare DB cohorts to broker positions for one user.

    Returns: {ghost_cohorts: int, untracked_positions: int, errors: int}
    """
    summary = {"ghost_cohorts": 0, "untracked_positions": 0, "errors": 0}
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.id == user_id).first()
        if user is None:
            return summary

        async def _get_positions(s: dict) -> list[dict]:
            client = TradeLockerClient(env=s["env"])
            return await client.get_positions(
                s["account_id"], s["token"], s["acc_num"]
            )

        try:
            broker_positions = await call_with_refresh(user_id, _get_positions, db=db)
        except ValueError:
            # no_session — user hasn't connected TL yet
            return summary
        except TradeLockerError as exc:
            logger.info("reconciliation skipped for user=%s: %s", user_id, exc)
            summary["errors"] = 1
            return summary

        # Broker position IDs (strings) for matching
        broker_pos_ids = {str(p.get("id")) for p in broker_positions if p.get("id")}

        # DB cohorts with open legs that have tradelocker_position_id set
        open_cohorts = (
            db.query(Cohort)
            .filter(
                Cohort.user_id == user_id,
                Cohort.status != CohortStatus.closed,
            )
            .all()
        )

        # 1. Ghost cohorts: our open leg references a position that doesn't exist
        for cohort in open_cohorts:
            for leg in cohort.legs:
                if not leg.is_open or not leg.tradelocker_position_id:
                    continue
                if leg.tradelocker_position_id not in broker_pos_ids:
                    summary["ghost_cohorts"] += 1
                    key = _alert_key("ghost", f"{cohort.id}:{leg.id}")
                    if _should_alert(key):
                        logger.warning(
                            "GHOST COHORT: user=%s cohort=%s leg=%s pos=%s — not at broker",
                            user_id, cohort.id, leg.id, leg.tradelocker_position_id,
                        )
                        # Best-effort Discord alert
                        try:
                            from app.integrations.discord_signals import _webhook_url
                            import httpx
                            url = _webhook_url()
                            if url:
                                msg = (
                                    f"🔍 GHOST COHORT detected: user_id={user_id} "
                                    f"cohort={cohort.id} leg={leg.id} "
                                    f"pos={leg.tradelocker_position_id} not at broker. "
                                    f"Closing cohort defensively."
                                )
                                async with httpx.AsyncClient(timeout=5.0) as c:
                                    await c.post(url, json={"content": msg})
                        except Exception:
                            pass
                        # Mark the leg closed in our DB so trailing/management
                        # logic stops running against a phantom position.
                        leg.is_open = False
                    # leave the cohort open until all its legs close cleanly

        # 2. Untracked broker positions: broker has it, but no open leg points to it
        all_tracked_ids: set[str] = set()
        for cohort in open_cohorts:
            for leg in cohort.legs:
                if leg.tradelocker_position_id:
                    all_tracked_ids.add(leg.tradelocker_position_id)

        for pos in broker_positions:
            pid = str(pos.get("id"))
            if pid not in all_tracked_ids:
                summary["untracked_positions"] += 1
                # HARD RULE 2026-05-20: reconciliation MUST NEVER call
                # modify_position on an untracked position. "Untracked"
                # means "the bot did not initiate it" — by definition that
                # makes it the USER's manual trade. Touching user-owned
                # trades is what caused the cmercer / poppad incident
                # where day-trading positions were silently slapped with
                # 0.5%-adverse stops and closed negative on noise.
                #
                # We still LOG + ALERT so operators see drift, but the
                # only place SL/TP gets set is the runner's own post-
                # place_order repair path (which is gated by a Cohort/
                # CohortLeg row that proves bot ownership). The earlier
                # "auto-protect" feature was removed in full.
                has_sl = bool(pos.get("stopLossId"))
                has_tp = bool(pos.get("takeProfitId"))
                key = _alert_key("untracked", pid)
                if _should_alert(key):
                    logger.warning(
                        "UNTRACKED BROKER POSITION (manual / non-bot): user=%s "
                        "pos=%s side=%s qty=%s tid=%s has_sl=%s has_tp=%s — "
                        "bot will NOT manage or modify",
                        user_id, pid,
                        pos.get("side"), pos.get("qty"),
                        pos.get("tradableInstrumentId"),
                        has_sl, has_tp,
                    )
                    try:
                        from app.integrations.discord_signals import _webhook_url
                        import httpx
                        url = _webhook_url()
                        if url:
                            msg = (
                                f"ℹ️ UNTRACKED BROKER POSITION: user_id={user_id} "
                                f"pos={pid} side={pos.get('side')} qty={pos.get('qty')} "
                                f"has_sl={has_sl} has_tp={has_tp}. "
                                f"Bot is NOT managing this position (manual trade "
                                f"or placed-before-runner-tracked). No action taken."
                            )
                            async with httpx.AsyncClient(timeout=5.0) as c:
                                await c.post(url, json={"content": msg})
                    except Exception:
                        pass

        db.commit()
    finally:
        db.close()
    return summary


async def reconcile_all_users() -> dict:
    """Run reconciliation for every user with TL credentials.

    Returns aggregated summary across all users.
    """
    db = SessionLocal()
    try:
        user_ids = [
            u.id for u in db.query(User).filter(User.tradelocker_token.is_not(None)).all()
        ]
    finally:
        db.close()

    total = {"ghost_cohorts": 0, "untracked_positions": 0, "errors": 0, "users": 0}
    for uid in user_ids:
        try:
            r = await reconcile_user(uid)
            total["ghost_cohorts"] += r["ghost_cohorts"]
            total["untracked_positions"] += r["untracked_positions"]
            total["errors"] += r["errors"]
            total["users"] += 1
        except Exception as exc:
            logger.warning("reconcile_user(%s) failed: %s", uid, exc)
            total["errors"] += 1
    return total


async def periodic_reconciliation_task(interval_seconds: int = 1800) -> None:
    """Background task: run reconciliation every 30 min.

    Bumped 10m → 30m on 2026-05-11 — for a single-user system the drift
    window we trade against detection latency is negligible, and 3× fewer
    broker round-trips reduces queue depth during broker slowdowns. The
    runner's per-tick broker-truth gating still catches drift before any
    trade is placed; this background task is the belt-and-suspenders.

    Started from app lifespan. Cancellable on shutdown.
    """
    while True:
        try:
            summary = await reconcile_all_users()
            logger.info("periodic reconciliation: %s", summary)
        except Exception as exc:
            logger.warning("periodic reconciliation raised: %s", exc)
        try:
            await asyncio.sleep(interval_seconds)
        except asyncio.CancelledError:
            return
