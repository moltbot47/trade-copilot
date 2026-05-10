"""RelayManager — tracks per-user TradeLocker poller tasks.

Singleton-style registry. The relay polls TradeLocker REST every ~2s on
behalf of an authenticated user and republishes deltas onto the WS
event bus (see app.ws.event_bus).

Usage:
    from app.ws.relay_manager import relay_manager
    relay_manager.start_for_user(user_id)   # idempotent
    relay_manager.stop_for_user(user_id)

Starting a relay is fire-and-forget — the asyncio task lives until the
user logs out, the relay errors fatally, or the process exits.

Wave 3B contract — see docs/WS_PROTOCOL.md §"TradeLocker relay contract".
"""
from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable, Optional

logger = logging.getLogger(__name__)


# Type alias for the factory that builds + runs a relay for a given user.
# Defaults to TradeLockerRelay.run, but tests can inject a stub.
RelayFactory = Callable[[int], Awaitable[None]]


async def _default_factory(user_id: int) -> None:
    # Imported lazily to avoid an import cycle (tradelocker_relay imports
    # event_bus, which is also imported via app.ws.__init__).
    from app.ws.tradelocker_relay import TradeLockerRelay

    relay = TradeLockerRelay(user_id=user_id)
    await relay.run()


class RelayManager:
    """Per-user task registry. Idempotent start/stop semantics."""

    def __init__(self, factory: Optional[RelayFactory] = None) -> None:
        self._tasks: dict[int, asyncio.Task] = {}
        self._factory: RelayFactory = factory or _default_factory
        self._lock = asyncio.Lock()

    def set_factory(self, factory: RelayFactory) -> None:
        """Override the relay factory (used in tests)."""
        self._factory = factory

    def start_for_user(self, user_id: int) -> Optional[asyncio.Task]:
        """Schedule a relay task for ``user_id``. Returns the asyncio.Task.

        Idempotent — if a live task already exists, returns it unchanged.
        Returns None when called outside an asyncio loop (logs a warning).
        """
        existing = self._tasks.get(user_id)
        if existing is not None and not existing.done():
            return existing

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            logger.warning(
                "relay_manager.start_for_user(user_id=%s): no running loop; skipping",
                user_id,
            )
            return None

        async def _run() -> None:
            await self._factory(user_id)

        task: asyncio.Task[None] = loop.create_task(
            _run(), name=f"tl-relay-{user_id}"
        )
        self._tasks[user_id] = task

        def _cleanup(t: asyncio.Task) -> None:
            # Only forget the slot if it's still ours.
            if self._tasks.get(user_id) is t:
                self._tasks.pop(user_id, None)
            if not t.cancelled():
                exc = t.exception()
                if exc is not None:
                    logger.error(
                        "tl-relay user=%s exited with exception: %r", user_id, exc
                    )

        task.add_done_callback(_cleanup)
        logger.info("tl-relay started user=%s", user_id)
        return task

    async def stop_for_user(self, user_id: int) -> None:
        """Cancel and clean up the relay task for ``user_id`` (idempotent)."""
        task = self._tasks.pop(user_id, None)
        if task is None:
            return
        if task.done():
            return
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass
        logger.info("tl-relay stopped user=%s", user_id)

    def is_running(self, user_id: int) -> bool:
        t = self._tasks.get(user_id)
        return bool(t and not t.done())

    def active_user_ids(self) -> list[int]:
        return [uid for uid, t in self._tasks.items() if not t.done()]

    async def stop_all(self) -> None:
        ids = list(self._tasks.keys())
        for uid in ids:
            await self.stop_for_user(uid)


# Process-wide singleton.
relay_manager = RelayManager()
