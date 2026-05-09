"""ConnectionManager — tracks live WS connections and per-user channel subs.

Protocol contract (docs/WS_PROTOCOL.md):
- Max 1 connection per user. When a new auth succeeds, any prior connection
  for that user is closed with code 1000.
- Each connection has a set of subscribed channels (max 16).
- Channels are user-scoped; nothing here prevents two different users
  subscribing to "account" — they each receive only their own events.

This module is in-memory only. Horizontal scaling will require replacing
the registry with a Redis-backed store and the event bus with Redis pub/sub.
"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    from fastapi import WebSocket

logger = logging.getLogger(__name__)

# Channel allowlist per protocol §"Subscribe / unsubscribe".
ALLOWED_CHANNELS: frozenset[str] = frozenset(
    {
        "account",
        "positions",
        "trades",
        "signals",
        "strategy:1m",
        "strategy:5m",
    }
)

MAX_SUBSCRIPTIONS_PER_CONN = 16
MAX_FRAMES_PER_SECOND = 10


@dataclass
class Connection:
    """Per-WebSocket bookkeeping."""

    conn_id: str
    ws: "WebSocket"
    user_id: int | None = None
    user_email: str | None = None
    subscribed: set[str] = field(default_factory=set)
    connected_at: float = field(default_factory=time.time)
    last_pong_at: float = field(default_factory=time.time)
    frames_in: int = 0
    frames_out: int = 0
    # Frame-rate window (sliding 1s).
    _frame_window_start: float = field(default_factory=time.time)
    _frames_in_window: int = 0

    def authenticated(self) -> bool:
        return self.user_id is not None

    def can_subscribe(self) -> bool:
        return len(self.subscribed) < MAX_SUBSCRIPTIONS_PER_CONN

    def admit_frame(self) -> bool:
        """Sliding 1-second window rate-limit. Returns False if over cap."""
        now = time.time()
        if now - self._frame_window_start >= 1.0:
            self._frame_window_start = now
            self._frames_in_window = 0
        self._frames_in_window += 1
        return self._frames_in_window <= MAX_FRAMES_PER_SECOND


class ConnectionManager:
    """Singleton-style registry. One instance per process."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        # conn_id → Connection
        self._connections: dict[str, Connection] = {}
        # user_id → conn_id (latest only — protocol mandates 1/user)
        self._by_user: dict[int, str] = {}

    async def add_connection(self, ws: "WebSocket") -> Connection:
        """Register a fresh (unauthenticated) connection. Returns the row."""
        conn = Connection(conn_id=str(uuid.uuid4()), ws=ws)
        async with self._lock:
            self._connections[conn.conn_id] = conn
        return conn

    async def authenticate(self, conn: Connection, user_id: int, email: str) -> Connection | None:
        """Promote a connection to authenticated and evict any prior conn for that user.

        Returns the previously-authenticated Connection if one was evicted (so the
        caller can close it), else None.
        """
        evicted: Connection | None = None
        async with self._lock:
            prior_conn_id = self._by_user.get(user_id)
            if prior_conn_id and prior_conn_id != conn.conn_id:
                evicted = self._connections.get(prior_conn_id)
            conn.user_id = user_id
            conn.user_email = email
            self._by_user[user_id] = conn.conn_id
        return evicted

    async def remove(self, conn_id: str) -> None:
        async with self._lock:
            conn = self._connections.pop(conn_id, None)
            if conn is None:
                return
            if conn.user_id is not None and self._by_user.get(conn.user_id) == conn_id:
                self._by_user.pop(conn.user_id, None)

    def get(self, conn_id: str) -> Connection | None:
        return self._connections.get(conn_id)

    def get_for_user(self, user_id: int) -> list["WebSocket"]:
        """Return all live WebSocket objects for a given user (0 or 1, by protocol)."""
        cid = self._by_user.get(user_id)
        if cid is None:
            return []
        conn = self._connections.get(cid)
        return [conn.ws] if conn else []

    def get_subscribers(self, channel: str, user_id: int) -> list[Connection]:
        """Return Connections for `user_id` that have subscribed to `channel`."""
        cid = self._by_user.get(user_id)
        if cid is None:
            return []
        conn = self._connections.get(cid)
        if conn is None or channel not in conn.subscribed:
            return []
        return [conn]

    def all_subscribers(self, channel: str) -> list[Connection]:
        """Return all authenticated connections subscribed to `channel`, across users.

        Used for system-wide broadcasts (publish_global)."""
        return [
            c
            for c in self._connections.values()
            if c.user_id is not None and channel in c.subscribed
        ]

    def total_connections(self) -> int:
        return len(self._connections)


# Process-wide singleton. Imported by event_bus and the /ws route.
connection_manager = ConnectionManager()
