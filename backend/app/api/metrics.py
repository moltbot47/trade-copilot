"""Prometheus /metrics endpoint + authenticated latency rollup endpoint.

The Prometheus `/metrics` route is unauthenticated — scraping is standard
practice over a private network/sidecar. If exposed publicly, gate it with
a reverse-proxy IP allowlist.

The latency rollup at `/api/metrics/latency` is mounted under the API
prefix in main.py and gated by `get_current_user` — same auth surface as
the rest of the dashboard endpoints.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends
from fastapi.responses import Response

from app.api.users import get_current_user
from app.core.metrics import render_prometheus
from app.db.models import User
from app.monitoring.latency import get_latency_tracker

router = APIRouter(tags=["metrics"])


@router.get("/metrics")
def metrics() -> Response:
    body, content_type = render_prometheus()
    return Response(content=body, media_type=content_type)


# Mounted under /api in main.py so the full path is /api/metrics/latency.
latency_router = APIRouter(prefix="/metrics", tags=["metrics"])


@latency_router.get("/latency")
def latency_summary(
    _user: User = Depends(get_current_user),
) -> dict[str, dict[str, float | int]]:
    return get_latency_tracker().summary_all()
