"""Inbound frame routing.

Each `handle_*` returns one of:
  - "ok"           → keep the connection open
  - "close_4401"   → caller closes with auth-failed code after sending the failure frame
  - "close_4408"   → heartbeat timeout
  - "close_4429"   → connection rate-limit exceeded (not used today;
                     individual frame violations send `error` and stay open)

The router itself doesn't differentiate beyond returning a short tag — the
server loop reads the tag, takes any required side-effect (e.g., closing
the WS with a specific code), and otherwise continues.
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any

from fastapi import WebSocket
from sqlalchemy.orm import Session

from app.core.jwt import JWTError, verify_session_token
from app.db.models import User
from app.ws.commands import dispatch as dispatch_command
from app.ws.connection_manager import (
    ALLOWED_CHANNELS,
    Connection,
    connection_manager,
)

logger = logging.getLogger(__name__)


async def _send(ws: WebSocket, payload: dict[str, Any], conn: Connection | None = None) -> None:
    await ws.send_text(json.dumps(payload))
    if conn is not None:
        conn.frames_out += 1


async def _send_error(
    ws: WebSocket, code: str, message: str, conn: Connection | None = None
) -> None:
    await _send(ws, {"type": "error", "code": code, "message": message}, conn)


# ---------------------------------------------------------------------------
# auth
# ---------------------------------------------------------------------------
async def handle_auth(
    conn: Connection, ws: WebSocket, frame: dict[str, Any], db: Session
) -> str:
    if conn.authenticated():
        await _send_error(ws, "bad_frame", "already authenticated", conn)
        return "ok"

    token = frame.get("token")
    if not isinstance(token, str) or not token:
        await _send(ws, {"type": "auth_failed", "reason": "missing_token"}, conn)
        return "close_4401"

    try:
        claims = verify_session_token(token)
    except JWTError as exc:
        await _send(ws, {"type": "auth_failed", "reason": f"invalid_token: {exc}"}, conn)
        return "close_4401"

    email = claims.get("sub")
    if not isinstance(email, str) or not email:
        await _send(ws, {"type": "auth_failed", "reason": "malformed_token"}, conn)
        return "close_4401"

    # Look up the user (no auto-create — login flow handles that).
    user = db.query(User).filter(User.email == email).first()

    if user is None:
        await _send(ws, {"type": "auth_failed", "reason": "user_not_found"}, conn)
        return "close_4401"

    evicted = await connection_manager.authenticate(conn, user.id, user.email)
    if evicted is not None:
        # Best-effort close of the prior connection. Per protocol a user may
        # have only one live socket; new connection wins.
        try:
            await evicted.ws.close(code=1000)
        except Exception:  # noqa: BLE001
            pass

    await _send(ws, {"type": "auth_ok", "email": user.email, "user_id": user.id}, conn)
    return "ok"


# ---------------------------------------------------------------------------
# subscribe / unsubscribe
# ---------------------------------------------------------------------------
async def handle_subscribe(conn: Connection, ws: WebSocket, frame: dict[str, Any]) -> str:
    channel = frame.get("channel")
    if not isinstance(channel, str) or channel not in ALLOWED_CHANNELS:
        await _send_error(
            ws, "bad_frame", f"channel must be one of {sorted(ALLOWED_CHANNELS)}", conn
        )
        return "ok"
    if not conn.can_subscribe():
        await _send_error(ws, "bad_frame", "subscription limit reached", conn)
        return "ok"
    conn.subscribed.add(channel)
    await _send(ws, {"type": "subscribed", "channel": channel}, conn)
    return "ok"


async def handle_unsubscribe(conn: Connection, ws: WebSocket, frame: dict[str, Any]) -> str:
    channel = frame.get("channel")
    if not isinstance(channel, str):
        await _send_error(ws, "bad_frame", "channel (str) required", conn)
        return "ok"
    conn.subscribed.discard(channel)
    await _send(ws, {"type": "unsubscribed", "channel": channel}, conn)
    return "ok"


# ---------------------------------------------------------------------------
# command
# ---------------------------------------------------------------------------
async def handle_command(
    conn: Connection, ws: WebSocket, frame: dict[str, Any], db: Session
) -> str:
    cmd_id = frame.get("cmd_id")
    name = frame.get("name")
    params = frame.get("params") or {}

    if not isinstance(cmd_id, str) or not cmd_id:
        await _send_error(ws, "bad_frame", "cmd_id (str) required", conn)
        return "ok"
    if not isinstance(name, str) or not name:
        await _send(
            ws,
            {
                "type": "command_error",
                "cmd_id": cmd_id,
                "code": "bad_frame",
                "message": "name (str) required",
            },
            conn,
        )
        return "ok"
    if not isinstance(params, dict):
        await _send(
            ws,
            {
                "type": "command_error",
                "cmd_id": cmd_id,
                "code": "bad_frame",
                "message": "params must be an object",
            },
            conn,
        )
        return "ok"

    user = db.query(User).filter(User.id == conn.user_id).first()
    if user is None:
        await _send(
            ws,
            {
                "type": "command_error",
                "cmd_id": cmd_id,
                "code": "unauthorized",
                "message": "user not found",
            },
            conn,
        )
        return "ok"
    ok, result = await dispatch_command(name, user, params, db)

    if ok:
        await _send(ws, {"type": "command_ok", "cmd_id": cmd_id, "result": result}, conn)
    else:
        await _send(
            ws,
            {
                "type": "command_error",
                "cmd_id": cmd_id,
                "code": result.get("code", "error"),
                "message": result.get("message", "command failed"),
            },
            conn,
        )
    return "ok"


# ---------------------------------------------------------------------------
# heartbeat
# ---------------------------------------------------------------------------
async def handle_ping(conn: Connection, ws: WebSocket, frame: dict[str, Any]) -> str:
    ts = frame.get("ts")
    if not isinstance(ts, (int, float)):
        ts = int(time.time() * 1000)
    await _send(ws, {"type": "pong", "ts": ts}, conn)
    return "ok"


async def handle_pong(conn: Connection, ws: WebSocket, frame: dict[str, Any]) -> str:
    conn.last_pong_at = time.time()
    return "ok"


# ---------------------------------------------------------------------------
# Top-level router
# ---------------------------------------------------------------------------

# Frames accepted before auth completes.
PRE_AUTH_TYPES = {"auth", "ping"}

ROUTES = {
    "auth": handle_auth,
    "subscribe": handle_subscribe,
    "unsubscribe": handle_unsubscribe,
    "command": handle_command,
    "ping": handle_ping,
    "pong": handle_pong,
}


async def route_frame(
    conn: Connection, ws: WebSocket, frame: dict[str, Any], db: Session
) -> str:
    ftype = frame.get("type")
    if not isinstance(ftype, str):
        await _send_error(ws, "bad_frame", "missing or non-string 'type'", conn)
        return "ok"
    if not conn.authenticated() and ftype not in PRE_AUTH_TYPES:
        await _send_error(ws, "unauthorized", "auth required before this frame", conn)
        return "ok"
    handler = ROUTES.get(ftype)
    if handler is None:
        await _send_error(ws, "bad_frame", f"unknown frame type '{ftype}'", conn)
        return "ok"
    # Only auth and command need DB access; the others ignore the param.
    if ftype in {"auth", "command"}:
        return await handler(conn, ws, frame, db)
    return await handler(conn, ws, frame)
