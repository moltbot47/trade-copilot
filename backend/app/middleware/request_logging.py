"""Request logging middleware.

Logs every request as a structured JSON record:
  { method, path, status, duration_ms, client_ip, user_id, trace_id }

Skips /health and /metrics to keep noise down. Adds X-Request-ID response header.
Plays well with Prometheus metrics in app.core.metrics.
"""
from __future__ import annotations

import logging
import time
import uuid
from typing import Awaitable, Callable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from app.core.metrics import request_count, request_duration_seconds

logger = logging.getLogger("app.request")

_SKIP_PATHS = {"/health", "/health/detail", "/metrics"}


def _extract_user_id(request: Request) -> str | None:
    """Best-effort extraction of user id from JWT cookie. Never raises."""
    try:
        from app.config import get_settings
        from app.core.jwt import verify_session_token, JWTError
    except Exception:
        return None

    try:
        settings = get_settings()
        token = request.cookies.get(settings.SESSION_COOKIE_NAME)
        if not token:
            auth = request.headers.get("authorization", "")
            if auth.lower().startswith("bearer "):
                token = auth.split(None, 1)[1]
        if not token:
            return None
        claims = verify_session_token(token)
        return claims.get("sub")
    except JWTError:
        return None
    except Exception:
        return None


class RequestLoggingMiddleware(BaseHTTPMiddleware):
    """ASGI middleware that logs every request and emits Prometheus metrics."""

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        path = request.url.path
        skip = path in _SKIP_PATHS

        trace_id = request.headers.get("x-request-id") or uuid.uuid4().hex
        client_ip = request.client.host if request.client else None
        method = request.method

        start = time.perf_counter()
        status_code = 500
        try:
            response = await call_next(request)
            status_code = response.status_code
        except Exception:
            duration_ms = (time.perf_counter() - start) * 1000.0
            if not skip:
                logger.exception(
                    "request failed",
                    extra={
                        "method": method,
                        "path": path,
                        "status": 500,
                        "duration_ms": round(duration_ms, 2),
                        "client_ip": client_ip,
                        "user_id": _extract_user_id(request),
                        "trace_id": trace_id,
                    },
                )
                try:
                    request_count.labels(method=method, path=path, status_code="500").inc()
                    request_duration_seconds.labels(method=method, path=path).observe(duration_ms / 1000.0)
                except Exception:
                    pass
            raise

        duration_ms = (time.perf_counter() - start) * 1000.0
        response.headers["X-Request-ID"] = trace_id

        if not skip:
            user_id = _extract_user_id(request)
            logger.info(
                "request",
                extra={
                    "method": method,
                    "path": path,
                    "status": status_code,
                    "duration_ms": round(duration_ms, 2),
                    "client_ip": client_ip,
                    "user_id": user_id,
                    "trace_id": trace_id,
                },
            )
            try:
                request_count.labels(method=method, path=path, status_code=str(status_code)).inc()
                request_duration_seconds.labels(method=method, path=path).observe(duration_ms / 1000.0)
            except Exception:
                pass

        return response
