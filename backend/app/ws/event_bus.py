"""EventBus — fan-out server-pushed events to subscribed WebSockets.

Protocol contract (docs/WS_PROTOCOL.md §"Server-pushed events"):
    { "type": "event", "channel": "<name>", "ts": <ms>, "data": {...} }

Design notes:
- In-memory only. Horizontal scaling will require swapping this for a
  Redis pub/sub backend; the public surface (publish/publish_global)
  should remain unchanged so callers don't have to be rewritten.
- Fan-out is non-blocking: per-recipient send errors are swallowed and
  logged, never propagated. One bad client cannot stall the bus.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

from app.ws.connection_manager import Connection, connection_manager

logger = logging.getLogger(__name__)


def _now_ms() -> int:
    return int(time.time() * 1000)


class EventBus:
    """Thin wrapper around connection_manager that emits typed event frames."""

    def __init__(self) -> None:
        # Reference held so tests can monkeypatch with a fake manager.
        self._manager = connection_manager

    async def _send(self, conn: Connection, payload: dict[str, Any]) -> None:
        try:
            await conn.ws.send_text(json.dumps(payload))
            conn.frames_out += 1
        except Exception as exc:  # noqa: BLE001 — never let one bad socket kill fan-out
            logger.debug(
                "event_bus.send_failed conn=%s user=%s err=%s",
                conn.conn_id,
                conn.user_id,
                exc,
            )

    async def publish(self, channel: str, user_id: int, data: dict[str, Any]) -> int:
        """Push an event frame to all sockets owned by `user_id` subscribed to `channel`.

        Returns the number of sockets the event was successfully dispatched to
        (best-effort — failures are not counted but do not propagate).
        """
        subs = self._manager.get_subscribers(channel, user_id)
        if not subs:
            return 0
        frame = {"type": "event", "channel": channel, "ts": _now_ms(), "data": data}
        results = await asyncio.gather(
            *(self._send(c, frame) for c in subs), return_exceptions=True
        )
        # Count non-exception results.
        return sum(1 for r in results if not isinstance(r, BaseException))

    async def publish_global(self, channel: str, data: dict[str, Any]) -> int:
        """Push an event frame to ALL authenticated connections subscribed to `channel`.

        Used for system-wide events (e.g., "TL bot status flipped" broadcasts).
        Per-user channels should still go through `publish` to keep scoping safe.
        """
        subs = self._manager.all_subscribers(channel)
        if not subs:
            return 0
        frame = {"type": "event", "channel": channel, "ts": _now_ms(), "data": data}
        results = await asyncio.gather(
            *(self._send(c, frame) for c in subs), return_exceptions=True
        )
        return sum(1 for r in results if not isinstance(r, BaseException))


# Process-wide singleton.
event_bus = EventBus()
