"""Fan signals out to all subscribers and place broker orders."""
from __future__ import annotations

import logging
from typing import Any

from sqlalchemy.orm import Session

from app.core.crypto import decrypt
from app.core.risk_engine import compute_user_lot
from app.core.tradelocker_client import TradeLockerClient, TradeLockerError
from app.db.models import Execution, ExecutionStatus, Signal, Subscription, User

logger = logging.getLogger(__name__)


async def fan_out(signal: Signal, db: Session) -> list[Execution]:
    """Place orders for every active subscriber of the signal's bot.

    Returns the list of Execution rows (already persisted).
    """
    subs = (
        db.query(Subscription)
        .filter(Subscription.bot_id == signal.bot_id, Subscription.is_paused.is_(False))
        .all()
    )

    executions: list[Execution] = []
    signal_symbol = (signal.instrument or "").strip().upper()

    for sub in subs:
        # Per-user instrument filter. NULL/empty means "all instruments" (legacy
        # behavior). A non-empty CSV narrows to the listed symbols. Skip silently
        # — no execution row, because the user explicitly opted out of this
        # instrument and a 'rejected' row would clutter their dashboard.
        allowed_raw = (sub.allowed_instruments or "").strip()
        if allowed_raw:
            allowed = {s.strip().upper() for s in allowed_raw.split(",") if s.strip()}
            if signal_symbol and signal_symbol not in allowed:
                logger.debug(
                    "skipping user=%s for signal %s: instrument not in user filter (%s)",
                    sub.user_id, signal_symbol, sorted(allowed),
                )
                continue

        user: User | None = db.get(User, sub.user_id)
        if user is None or not user.is_active:
            continue

        token = decrypt(user.tradelocker_token) if user.tradelocker_token else None
        account_id = user.tradelocker_account_id
        acc_num = user.tradelocker_acc_num

        if not token or not account_id:
            ex = Execution(
                signal_id=signal.id,
                user_id=user.id,
                status=ExecutionStatus.rejected,
                error_message="user has no connected TradeLocker account",
            )
            db.add(ex)
            executions.append(ex)
            continue

        client = TradeLockerClient(env=user.tradelocker_env or "demo")

        # Pull live balance for risk sizing; fall back to 10k if unavailable.
        balance = 10_000.0
        try:
            state: Any = await client.get_account_state(account_id, token, acc_num or "1")
            balance = float(
                state.get("balance")
                or state.get("equity")
                or (state.get("accountState", {}).get("balance") if isinstance(state, dict) else 0)
                or 10_000.0
            )
        except TradeLockerError as exc:
            logger.info("balance lookup failed for user=%s: %s", user.id, exc)

        lot = compute_user_lot(
            base_lot=signal.base_lot_size,
            aggression_level=sub.aggression_level,
            account_balance=balance,
            max_risk_pct=user.max_daily_loss_pct,
        )

        ex = Execution(
            signal_id=signal.id,
            user_id=user.id,
            status=ExecutionStatus.pending,
            executed_lot_size=lot,
        )
        db.add(ex)
        db.flush()  # get ex.id

        try:
            order = await client.place_order(
                account_id=account_id,
                token=token,
                acc_num=acc_num or "1",
                symbol=signal.instrument,
                side=signal.side,
                qty=lot,
                sl=signal.stop_loss,
                tp=signal.take_profit,
            )
            order_id = (
                str(order.get("order_id") or order.get("orderId") or order.get("id") or "")
                if isinstance(order, dict)
                else ""
            )
            ex.tradelocker_order_id = order_id or None
            ex.status = ExecutionStatus.filled
            ex.fill_price = signal.entry_price
        except TradeLockerError as exc:
            ex.status = ExecutionStatus.error
            ex.error_message = str(exc)[:500]
            logger.warning("order failed for user=%s: %s", user.id, exc)

        executions.append(ex)

    db.commit()
    return executions
