"""WebSocket layer — protocol contract: docs/WS_PROTOCOL.md.

Public surface:
- `event_bus`: in-memory pub/sub for server-pushed events. (Wave 3A)
- `connection_manager`: per-user/connection registry. (Wave 3A)
- `router`: FastAPI APIRouter mounting the `/ws` upgrade endpoint. (Wave 3A)
- `relay_manager`: per-user TradeLocker poller registry. (Wave 3B)
- `TradeLockerRelay`: REST→WS bridge poller. (Wave 3B)
"""
from __future__ import annotations

from app.ws.connection_manager import ConnectionManager, connection_manager
from app.ws.relay_manager import RelayManager, relay_manager
from app.ws.tradelocker_relay import TradeLockerRelay

# event_bus + server come from Wave 3A. Import defensively so 3B-only
# environments (and earlier 3A iterations without server.py) still load.
try:  # pragma: no cover — wired up once Wave 3A lands
    from app.ws.event_bus import EventBus, event_bus  # noqa: F401
except Exception:  # pragma: no cover
    EventBus = None  # type: ignore[assignment]
    event_bus = None  # type: ignore[assignment]

try:  # pragma: no cover
    from app.ws.server import router  # noqa: F401
except Exception:  # pragma: no cover
    router = None  # type: ignore[assignment]

__all__ = [
    "ConnectionManager",
    "EventBus",
    "RelayManager",
    "TradeLockerRelay",
    "connection_manager",
    "event_bus",
    "relay_manager",
    "router",
]
