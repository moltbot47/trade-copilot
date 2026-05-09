"""FastAPI `/ws` upgrade endpoint.

Per docs/WS_PROTOCOL.md:
- 30s auth timeout — close 4401 if first frame isn't a successful `auth`.
- Server `ping` every 30s; if no `pong` within 10s → close 4408.
- Frame-rate cap: 10/sec/conn. Over → `error` "rate_limited", drop the
  offending frame, keep the connection open.
- Structured connect/disconnect logs (user_id, duration, frames_in/out).
"""
from __future__ import annotations

import asyncio
import json
import logging
import time

from fastapi import APIRouter, Depends, WebSocket, WebSocketDisconnect
from sqlalchemy.orm import Session

from app.db.database import get_db
from app.ws.connection_manager import Connection, connection_manager
from app.ws.handlers import _send, _send_error, route_frame

logger = logging.getLogger(__name__)

router = APIRouter()

# Protocol-defined timings (override-friendly for tests).
AUTH_TIMEOUT_SEC = 30.0
PING_INTERVAL_SEC = 30.0
PONG_TIMEOUT_SEC = 10.0


# ---------------------------------------------------------------------------
# Background tasks
# ---------------------------------------------------------------------------
async def _auth_watchdog(conn: Connection, ws: WebSocket) -> None:
    """If conn is still unauthenticated after AUTH_TIMEOUT_SEC, kick it."""
    try:
        await asyncio.sleep(AUTH_TIMEOUT_SEC)
    except asyncio.CancelledError:
        return
    if not conn.authenticated():
        try:
            await _send(ws, {"type": "auth_failed", "reason": "auth_timeout"}, conn)
        except Exception:  # noqa: BLE001
            pass
        try:
            await ws.close(code=4401)
        except Exception:  # noqa: BLE001
            pass


async def _heartbeat(conn: Connection, ws: WebSocket) -> None:
    """Send pings every PING_INTERVAL_SEC; close 4408 if no pong within PONG_TIMEOUT_SEC."""
    while True:
        try:
            await asyncio.sleep(PING_INTERVAL_SEC)
        except asyncio.CancelledError:
            return
        if not conn.authenticated():
            continue
        ts = int(time.time() * 1000)
        try:
            await _send(ws, {"type": "ping", "ts": ts}, conn)
        except Exception:  # noqa: BLE001
            return
        # Wait for pong window.
        deadline = time.time() + PONG_TIMEOUT_SEC
        while time.time() < deadline:
            try:
                await asyncio.sleep(0.5)
            except asyncio.CancelledError:
                return
            if conn.last_pong_at >= ts / 1000.0:
                break
        else:
            # Loop fell through — no pong in time.
            try:
                await ws.close(code=4408)
            except Exception:  # noqa: BLE001
                pass
            return


# ---------------------------------------------------------------------------
# Main /ws route
# ---------------------------------------------------------------------------
@router.websocket("/ws")
async def websocket_endpoint(
    ws: WebSocket,
    db: Session = Depends(get_db),
) -> None:
    await ws.accept()
    conn = await connection_manager.add_connection(ws)
    started = time.time()
    client_ip = ws.client.host if ws.client else "?"

    logger.info(
        "ws.connect conn_id=%s ip=%s",
        conn.conn_id,
        client_ip,
        extra={"event": "ws.connect", "conn_id": conn.conn_id, "ip": client_ip},
    )

    auth_task = asyncio.create_task(_auth_watchdog(conn, ws))
    heartbeat_task = asyncio.create_task(_heartbeat(conn, ws))

    close_code: int | None = None
    try:
        while True:
            raw = await ws.receive_text()
            conn.frames_in += 1

            # Frame-rate cap.
            if not conn.admit_frame():
                try:
                    await _send_error(ws, "rate_limited", "frame rate exceeded", conn)
                except Exception:  # noqa: BLE001
                    break
                continue

            try:
                frame = json.loads(raw)
            except json.JSONDecodeError:
                await _send_error(ws, "bad_frame", "invalid JSON", conn)
                continue
            if not isinstance(frame, dict):
                await _send_error(ws, "bad_frame", "frame must be a JSON object", conn)
                continue

            verdict = await route_frame(conn, ws, frame, db)
            if verdict == "close_4401":
                close_code = 4401
                break
            if verdict == "close_4408":
                close_code = 4408
                break
            if verdict == "close_4429":
                close_code = 4429
                break
    except WebSocketDisconnect:
        pass
    except Exception as exc:  # noqa: BLE001
        logger.exception("ws.error conn_id=%s err=%s", conn.conn_id, exc)
    finally:
        auth_task.cancel()
        heartbeat_task.cancel()
        if close_code is not None:
            try:
                await ws.close(code=close_code)
            except Exception:  # noqa: BLE001
                pass
        await connection_manager.remove(conn.conn_id)
        duration_ms = int((time.time() - started) * 1000)
        logger.info(
            "ws.disconnect conn_id=%s user_id=%s duration_ms=%s frames_in=%s frames_out=%s",
            conn.conn_id,
            conn.user_id,
            duration_ms,
            conn.frames_in,
            conn.frames_out,
            extra={
                "event": "ws.disconnect",
                "conn_id": conn.conn_id,
                "user_id": conn.user_id,
                "duration_ms": duration_ms,
                "frames_in": conn.frames_in,
                "frames_out": conn.frames_out,
            },
        )
