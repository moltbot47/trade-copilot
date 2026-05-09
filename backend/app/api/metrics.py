"""Prometheus /metrics endpoint.

No auth — Prometheus scraping is standard practice over private network/sidecar.
If exposed publicly, gate with reverse-proxy IP allowlist.
"""
from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import Response

from app.core.metrics import render_prometheus

router = APIRouter(tags=["metrics"])


@router.get("/metrics")
def metrics() -> Response:
    body, content_type = render_prometheus()
    return Response(content=body, media_type=content_type)
