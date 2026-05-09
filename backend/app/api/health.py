"""Health endpoints.

- GET /health         — liveness probe, always cheap, returns 200 if process alive.
- GET /health/detail  — readiness probe with DB query + TradeLocker connectivity check.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import httpx
from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db.database import get_db

logger = logging.getLogger(__name__)
router = APIRouter(tags=["health"])

_BOOT_TS = time.time()
_VERSION = "0.1.0"


@router.get("/health")
def health() -> dict[str, str]:
    """Liveness — never touches IO. K8s liveness probe target."""
    return {"status": "ok"}


async def _check_tradelocker(timeout: float = 2.0) -> dict[str, Any]:
    """HEAD/GET on TradeLocker demo auth root — connectivity only, no creds."""
    settings = get_settings()
    base = settings.TRADELOCKER_DEMO_API_BASE.rstrip("/")
    started = time.perf_counter()
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.get(base, follow_redirects=False)
        latency_ms = round((time.perf_counter() - started) * 1000.0, 2)
        # 2xx/3xx/4xx = reachable. Only 5xx or transport failure = down.
        if resp.status_code < 500:
            return {"status": "ok", "latency_ms": latency_ms, "code": resp.status_code}
        return {"status": "fail", "latency_ms": latency_ms, "code": resp.status_code}
    except (httpx.TimeoutException, asyncio.TimeoutError):
        return {"status": "fail", "latency_ms": round((time.perf_counter() - started) * 1000.0, 2), "error": "timeout"}
    except Exception as exc:
        return {"status": "fail", "latency_ms": round((time.perf_counter() - started) * 1000.0, 2), "error": str(exc)[:200]}


def _check_database(db: Session) -> dict[str, Any]:
    started = time.perf_counter()
    try:
        db.execute(text("SELECT 1"))
        latency_ms = round((time.perf_counter() - started) * 1000.0, 2)
        return {"status": "ok", "latency_ms": latency_ms}
    except Exception as exc:
        latency_ms = round((time.perf_counter() - started) * 1000.0, 2)
        logger.warning("db health probe failed: %s", exc)
        return {"status": "fail", "latency_ms": latency_ms, "error": str(exc)[:200]}


@router.get("/health/detail")
async def health_detail(db: Session = Depends(get_db)) -> JSONResponse:
    """Readiness probe — checks DB + TradeLocker reachability."""
    db_check = _check_database(db)
    tl_check = await _check_tradelocker()

    statuses = [db_check["status"], tl_check["status"]]
    if "fail" in statuses and db_check["status"] == "fail":
        overall = "down"
        http_status = 503
    elif "fail" in statuses:
        overall = "degraded"
        http_status = 200
    else:
        overall = "ok"
        http_status = 200

    body = {
        "status": overall,
        "checks": {
            "database": db_check,
            "tradelocker_demo": tl_check,
        },
        "version": _VERSION,
        "uptime_seconds": int(time.time() - _BOOT_TS),
    }
    return JSONResponse(status_code=http_status, content=body)
