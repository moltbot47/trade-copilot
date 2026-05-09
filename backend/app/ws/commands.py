"""Command RPC handlers for WS frames of `type: "command"`.

Each handler is `async def handle(user, params, db) -> tuple[bool, dict]`:
- ok=True, result_dict     → server replies command_ok
- ok=False, error_dict     → server replies command_error
                              (`code`/`message` keys consumed by handlers.py)

All handlers MUST validate ownership: a user can only act on their own
subscriptions, and start/stop is gated by the existing strategy validation.
"""
from __future__ import annotations

import logging
from typing import Any, Callable, Awaitable

from sqlalchemy.orm import Session

from app.db.database import SessionLocal
from app.db.models import Bot, StrategyType, Subscription, User

logger = logging.getLogger(__name__)

CommandResult = tuple[bool, dict[str, Any]]
CommandHandler = Callable[[User, dict[str, Any], Session], Awaitable[CommandResult]]


def _err(code: str, message: str) -> dict[str, Any]:
    return {"code": code, "message": message}


# ---------------------------------------------------------------------------
# strategy.start
# ---------------------------------------------------------------------------
async def handle_strategy_start(user: User, params: dict[str, Any], db: Session) -> CommandResult:
    bot_id = params.get("bot_id")
    timeframe = params.get("timeframe", "1m")
    symbols = params.get("symbols")
    latpfn_endpoint = params.get("latpfn_endpoint")

    if not isinstance(bot_id, int):
        return False, _err("validation", "bot_id (int) required")
    if not isinstance(timeframe, str) or timeframe not in {"1m", "5m"}:
        return False, _err("validation", "timeframe must be '1m' or '5m'")
    if not isinstance(symbols, list) or not symbols or not all(isinstance(s, str) for s in symbols):
        return False, _err("validation", "symbols must be a non-empty list[str]")

    bot = db.get(Bot, bot_id)
    if bot is None:
        return False, _err("not_found", "bot not found")
    if bot.strategy_type != StrategyType.latpfn_momentum:
        return False, _err("validation", "this strategy runner only supports latpfn_momentum bots")

    # Lazy import — keeps the module import-cheap and avoids any circulars
    # with the strategies package.
    from app.strategies.runner import StrategyRunner

    try:
        runner = await StrategyRunner.start(
            db_session_factory=SessionLocal,
            bot_id=bot_id,
            timeframe=timeframe,
            symbols=symbols,
            user_emails=[user.email],
            latpfn_endpoint=latpfn_endpoint,
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("ws strategy.start failed bot_id=%s", bot_id)
        return False, _err("internal", f"runner start failed: {exc}")

    return True, {
        "status": "started",
        "bot_id": runner.bot_id,
        "timeframe": runner.timeframe,
        "symbols": runner.symbols,
        "task_alive": runner.task is not None and not runner.task.done(),
    }


# ---------------------------------------------------------------------------
# strategy.stop
# ---------------------------------------------------------------------------
async def handle_strategy_stop(user: User, params: dict[str, Any], db: Session) -> CommandResult:
    bot_id = params.get("bot_id")
    timeframe = params.get("timeframe", "1m")

    if not isinstance(bot_id, int):
        return False, _err("validation", "bot_id (int) required")
    if not isinstance(timeframe, str):
        return False, _err("validation", "timeframe must be a string")

    from app.strategies.runner import get_runner

    runner = get_runner(bot_id, timeframe)
    if runner is None:
        return True, {"status": "not_running", "bot_id": bot_id, "timeframe": timeframe}
    try:
        await runner.stop()
    except Exception as exc:  # noqa: BLE001
        logger.exception("ws strategy.stop failed bot_id=%s", bot_id)
        return False, _err("internal", f"runner stop failed: {exc}")
    return True, {"status": "stopped", "bot_id": bot_id, "timeframe": timeframe}


# ---------------------------------------------------------------------------
# subscription.update
# ---------------------------------------------------------------------------
async def handle_subscription_update(
    user: User, params: dict[str, Any], db: Session
) -> CommandResult:
    sub_id = params.get("subscription_id")
    if not isinstance(sub_id, int):
        return False, _err("validation", "subscription_id (int) required")

    sub = db.get(Subscription, sub_id)
    if sub is None:
        return False, _err("not_found", "subscription not found")
    # Ownership enforcement — never let user A mutate user B's subscription.
    if sub.user_id != user.id:
        return False, _err("forbidden", "subscription does not belong to user")

    aggression = params.get("aggression_level")
    is_paused = params.get("is_paused")

    if aggression is not None:
        if not isinstance(aggression, int) or not (1 <= aggression <= 10):
            return False, _err("validation", "aggression_level must be int in [1, 10]")
        sub.aggression_level = aggression
    if is_paused is not None:
        if not isinstance(is_paused, bool):
            return False, _err("validation", "is_paused must be bool")
        sub.is_paused = is_paused

    db.add(sub)
    db.commit()
    db.refresh(sub)
    return True, {
        "subscription_id": sub.id,
        "user_id": sub.user_id,
        "bot_id": sub.bot_id,
        "aggression_level": sub.aggression_level,
        "is_paused": sub.is_paused,
        "updated_at": sub.updated_at.isoformat() if sub.updated_at else None,
    }


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------
COMMANDS: dict[str, CommandHandler] = {
    "strategy.start": handle_strategy_start,
    "strategy.stop": handle_strategy_stop,
    "subscription.update": handle_subscription_update,
}


async def dispatch(name: str, user: User, params: dict[str, Any], db: Session) -> CommandResult:
    handler = COMMANDS.get(name)
    if handler is None:
        return False, _err("unknown_command", f"command '{name}' not registered")
    try:
        return await handler(user, params or {}, db)
    except Exception as exc:  # noqa: BLE001 — last-line of defence
        logger.exception("ws command dispatch failure name=%s user=%s", name, user.id)
        return False, _err("internal", str(exc))
