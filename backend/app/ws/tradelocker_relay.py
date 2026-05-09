"""TradeLockerRelay — polls TL REST and pushes deltas onto the WS event bus.

Per active user, runs an asyncio loop:

  1. Load encrypted TL token from DB; decrypt.
  2. Every ~2s:
       * GET /trade/accounts/{id}/state — diff vs last → publish "account"
       * GET /trade/accounts/{id}/positions — diff vs last → publish "positions"
         events with kind={opened, closed, updated}.
  3. On 401 → try refresh_access_token; on persistent failure publish a global
     "error" event and back off 5min before retrying.
  4. Any other exception is logged; the loop never crashes.
  5. Cancellation = clean shutdown.

The mapping from raw TL state → published "data" payload mirrors the
existing /api/tradelocker/account REST endpoint (see app.api.tradelocker).

Discovery note (2026-05-08): TradeLocker exposes a public REST API
(https://public-api.tradelocker.com/) but the WebSocket streaming
endpoint is undocumented in the public docs — Genesis FX's published
material covers `/auth/jwt/*` and `/trade/*` REST paths only. If/when
a WS URL is confirmed (the embedded TradingView frame at
demo.tradelocker.com/charting connects to wss://demo.tradelocker.com/...
for prices, but no public spec exists), this poller can be replaced
with a subscription-based listener. Until then, REST polling at 2s is
the contract documented in docs/WS_PROTOCOL.md §"TradeLocker relay
contract".
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Optional

from app.core.crypto import decrypt, encrypt
from app.core.tradelocker_client import TradeLockerClient, TradeLockerError
from app.db.database import SessionLocal
from app.db.models import User

logger = logging.getLogger(__name__)


# --------- Tunables ---------
POLL_INTERVAL_S = 2.0
UPDATE_DEBOUNCE_S = 5.0
AUTH_BACKOFF_S = 300.0  # 5min after persistent 401
TRANSIENT_BACKOFF_S = 5.0  # short pause after a transient error


def _publish(channel: str, user_id: int, payload: dict) -> None:
    """Best-effort publish; never raise back into the relay loop.

    event_bus may not exist yet (Wave 3A in flight). Import lazily so
    a missing module doesn't crash the whole strategy/relay surface.
    """
    try:
        from app.ws.event_bus import event_bus
    except Exception as exc:  # pragma: no cover — defensive
        logger.debug("event_bus unavailable, skipping publish (%s)", exc)
        return
    try:
        result = event_bus.publish(channel, user_id, payload)
        if asyncio.iscoroutine(result):
            asyncio.ensure_future(result)
    except Exception as exc:
        logger.warning("event_bus.publish failed channel=%s: %s", channel, exc)


def _publish_global(channel: str, payload: dict) -> None:
    try:
        from app.ws.event_bus import event_bus
    except Exception as exc:  # pragma: no cover
        logger.debug("event_bus unavailable, skipping publish_global (%s)", exc)
        return
    try:
        result = event_bus.publish_global(channel, payload)
        if asyncio.iscoroutine(result):
            asyncio.ensure_future(result)
    except Exception as exc:
        logger.warning("event_bus.publish_global failed channel=%s: %s", channel, exc)


def map_account_state(state: dict[str, Any]) -> dict[str, Any]:
    """Project raw TL state → the WS "account" event-data shape.

    Mirrors app/api/tradelocker.py:account_state response model.
    """
    if not isinstance(state, dict):
        return {}
    return {
        "balance": state.get("balance"),
        # TradeLocker doesn't expose "equity" directly. projectedBalance is
        # closest (cash + unrealized PnL).
        "equity": state.get("projectedBalance") or state.get("balance"),
        "available_funds": state.get("availableFunds"),
        "open_pnl": state.get("openGrossPnL"),
        "today_net": state.get("todayNet"),
        "positions_count": int(state.get("positionsCount") or 0),
    }


def map_position(pos: dict[str, Any]) -> dict[str, Any]:
    """Project a TL position row → WS "positions" data payload (sans "kind")."""
    if not isinstance(pos, dict):
        return {}
    try:
        qty = float(pos.get("qty") or 0)
    except (TypeError, ValueError):
        qty = 0.0
    try:
        avg_price = float(pos.get("avgPrice") or 0) if pos.get("avgPrice") is not None else None
    except (TypeError, ValueError):
        avg_price = None
    try:
        unrealized = (
            float(pos.get("unrealizedPl"))
            if pos.get("unrealizedPl") is not None
            else None
        )
    except (TypeError, ValueError):
        unrealized = None
    return {
        "id": str(pos.get("id")) if pos.get("id") is not None else "",
        "symbol": pos.get("tradableInstrumentId"),  # numeric id; symbol resolution upstream
        "side": pos.get("side"),
        "qty": qty,
        "avg_price": avg_price,
        "unrealized_pl": unrealized,
    }


def diff_positions(
    prev: dict[str, dict[str, Any]],
    curr: dict[str, dict[str, Any]],
    last_update_emit: dict[str, float],
    now: Optional[float] = None,
    debounce_s: float = UPDATE_DEBOUNCE_S,
) -> list[dict[str, Any]]:
    """Compute the list of position events to emit between two snapshots.

    Args:
        prev: previous {position_id → mapped_payload}
        curr: current {position_id → mapped_payload}
        last_update_emit: {position_id → last "updated" emission ts (float seconds)},
            mutated in-place to track debounce state.
        now: current time (float seconds); defaults to time.time().
        debounce_s: minimum gap between successive "updated" emits per id.
    """
    if now is None:
        now = time.time()
    events: list[dict[str, Any]] = []

    # Opened: in curr, not prev.
    for pid, payload in curr.items():
        if pid not in prev:
            events.append({"kind": "opened", **payload})

    # Closed: in prev, not curr.
    for pid, payload in prev.items():
        if pid not in curr:
            events.append({"kind": "closed", **payload})
            last_update_emit.pop(pid, None)

    # Updated: in both, but qty/unrealized_pl changed → debounced.
    for pid, payload in curr.items():
        if pid not in prev:
            continue
        before = prev[pid]
        qty_changed = before.get("qty") != payload.get("qty")
        pnl_changed = before.get("unrealized_pl") != payload.get("unrealized_pl")
        if not (qty_changed or pnl_changed):
            continue
        last_at = last_update_emit.get(pid, 0.0)
        if now - last_at < debounce_s:
            continue
        last_update_emit[pid] = now
        events.append({"kind": "updated", **payload})

    return events


class TradeLockerRelay:
    """Per-user relay loop. Construct then await relay.run() (or hand it to
    relay_manager.start_for_user).
    """

    def __init__(
        self,
        user_id: int,
        client: Optional[TradeLockerClient] = None,
        poll_interval_s: float = POLL_INTERVAL_S,
        session_factory=None,
    ) -> None:
        self.user_id = user_id
        self._client = client
        self._poll_interval_s = poll_interval_s
        self._session_factory = session_factory or SessionLocal
        self._stop = asyncio.Event()
        # Snapshots/state across iterations:
        self._last_account_payload: Optional[dict[str, Any]] = None
        self._last_positions: dict[str, dict[str, Any]] = {}
        self._last_update_emit: dict[str, float] = {}

    def stop(self) -> None:
        self._stop.set()

    # ---------- credentials ----------

    def _load_creds(self) -> Optional[dict[str, Any]]:
        """Load + decrypt TL creds for this user. Returns None if not connected."""
        db = self._session_factory()
        try:
            user = db.get(User, self.user_id)
            if user is None or not user.tradelocker_token or not user.tradelocker_account_id:
                return None
            token = decrypt(user.tradelocker_token)
            refresh = decrypt(user.tradelocker_refresh_token) if user.tradelocker_refresh_token else None
            if not token:
                return None
            return {
                "token": token,
                "refresh": refresh,
                "account_id": user.tradelocker_account_id,
                "acc_num": user.tradelocker_acc_num or "1",
                "env": user.tradelocker_env or "demo",
            }
        finally:
            db.close()

    def _persist_refreshed(self, access: str, refresh: Optional[str]) -> None:
        db = self._session_factory()
        try:
            user = db.get(User, self.user_id)
            if user is None:
                return
            user.tradelocker_token = encrypt(access)
            if refresh:
                user.tradelocker_refresh_token = encrypt(refresh)
            db.commit()
        finally:
            db.close()

    async def _try_refresh(
        self, client: TradeLockerClient, refresh_token: Optional[str]
    ) -> Optional[str]:
        """Returns a new access token, or None on failure."""
        if not refresh_token:
            return None
        try:
            data = await client.refresh_access_token(refresh_token)
        except TradeLockerError as exc:
            logger.warning("tl-relay user=%s refresh failed: %s", self.user_id, exc)
            return None
        new_access = data.get("access_token")
        new_refresh = data.get("refresh_token")
        if new_access:
            self._persist_refreshed(new_access, new_refresh)
        return new_access

    # ---------- single iteration ----------

    async def poll_once(
        self,
        client: TradeLockerClient,
        creds: dict[str, Any],
    ) -> None:
        """Run one poll cycle (state + positions). Publishes deltas."""
        # 1) Account state
        try:
            raw_state = await client.get_account_state(
                creds["account_id"], creds["token"], creds["acc_num"]
            )
        except TradeLockerError:
            raise  # let run() handle 401/transient logic centrally
        mapped = map_account_state(raw_state or {})
        if mapped and mapped != self._last_account_payload:
            _publish("account", self.user_id, mapped)
            self._last_account_payload = mapped

        # 2) Positions
        try:
            raw_positions = await client.get_positions(
                creds["account_id"], creds["token"], creds["acc_num"]
            )
        except TradeLockerError:
            raise
        curr: dict[str, dict[str, Any]] = {}
        for row in raw_positions or []:
            payload = map_position(row)
            pid = payload.get("id")
            if not pid:
                continue
            curr[pid] = payload

        events = diff_positions(
            self._last_positions, curr, self._last_update_emit
        )
        for ev in events:
            _publish("positions", self.user_id, ev)
        self._last_positions = curr

    # ---------- main loop ----------

    async def run(self) -> None:
        """Run the relay until cancelled. Never raises into the caller."""
        client = self._client or TradeLockerClient(env="demo")
        # Re-fetch creds at the top of each iteration so token rotations
        # done elsewhere (manual reconnect) are picked up.
        try:
            while not self._stop.is_set():
                creds = self._load_creds()
                if not creds:
                    # User has no TL connection; pause and re-check periodically.
                    if await self._sleep_or_stop(self._poll_interval_s * 5):
                        return
                    continue

                # Honor env-per-user.
                if client.env != creds["env"]:
                    client = self._client or TradeLockerClient(env=creds["env"])

                try:
                    await self.poll_once(client, creds)
                except TradeLockerError as exc:
                    msg = str(exc).lower()
                    if "unauthorized" in msg or "401" in msg:
                        new_access = await self._try_refresh(client, creds.get("refresh"))
                        if new_access:
                            # Retry next iteration with the freshly persisted token
                            logger.info("tl-relay user=%s refreshed access token", self.user_id)
                            continue
                        # Persistent 401 — emit error and back off
                        _publish_global(
                            "error",
                            {
                                "scope": "tradelocker_relay",
                                "user_id": self.user_id,
                                "code": "unauthorized",
                                "message": "TradeLocker token rejected; reconnect required.",
                            },
                        )
                        if await self._sleep_or_stop(AUTH_BACKOFF_S):
                            return
                        continue
                    # Transient error
                    logger.info("tl-relay user=%s transient error: %s", self.user_id, exc)
                    if await self._sleep_or_stop(TRANSIENT_BACKOFF_S):
                        return
                    continue
                except Exception as exc:  # pragma: no cover — never crash
                    logger.exception("tl-relay user=%s unexpected: %s", self.user_id, exc)
                    if await self._sleep_or_stop(TRANSIENT_BACKOFF_S):
                        return
                    continue

                if await self._sleep_or_stop(self._poll_interval_s):
                    return
        except asyncio.CancelledError:
            logger.info("tl-relay user=%s cancelled", self.user_id)
            raise

    async def _sleep_or_stop(self, seconds: float) -> bool:
        """Sleep up to `seconds`, returning True if a stop was signalled."""
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=seconds)
            return True
        except asyncio.TimeoutError:
            return False
