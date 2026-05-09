#!/usr/bin/env python3
"""External health monitor — calls /health/detail and emits one-line JSON.

Designed for cron or external uptime checkers. Output goes to stdout for log
aggregation. Exit codes:
    0 — ok
    1 — degraded (some checks failed but service responsive)
    2 — down (DB failure, 5xx, or unreachable)

Example cron entry (every minute):
    * * * * * BACKEND_URL=https://api.tradecopilot.app /usr/bin/python3 \
        /opt/trade-copilot/backend/scripts/health_monitor.py >> /var/log/tc-health.log 2>&1

Usage:
    python health_monitor.py --url http://localhost:8000 --timeout 5
    BACKEND_URL=http://localhost:8000 python health_monitor.py
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from typing import Any
from urllib import error, request

EXIT_OK = 0
EXIT_DEGRADED = 1
EXIT_DOWN = 2


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="External health monitor for Trade Copilot.")
    p.add_argument(
        "--url",
        default=os.getenv("BACKEND_URL", "http://localhost:8000"),
        help="Backend base URL (default: $BACKEND_URL or http://localhost:8000)",
    )
    p.add_argument(
        "--timeout",
        type=float,
        default=float(os.getenv("HEALTH_TIMEOUT", "5.0")),
        help="Request timeout seconds (default 5).",
    )
    p.add_argument("--path", default="/health/detail", help="Health path (default /health/detail).")
    return p.parse_args()


def probe(base: str, path: str, timeout: float) -> tuple[int, dict[str, Any], float]:
    url = base.rstrip("/") + path
    started = time.perf_counter()
    req = request.Request(url, headers={"User-Agent": "tc-health-monitor/1.0"})
    try:
        with request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            latency_ms = (time.perf_counter() - started) * 1000.0
            try:
                data = json.loads(body)
            except json.JSONDecodeError:
                data = {"raw": body[:500]}
            return resp.status, data, latency_ms
    except error.HTTPError as e:
        latency_ms = (time.perf_counter() - started) * 1000.0
        try:
            data = json.loads(e.read().decode("utf-8", errors="replace"))
        except Exception:
            data = {"error": str(e)}
        return e.code, data, latency_ms
    except (error.URLError, TimeoutError) as e:
        latency_ms = (time.perf_counter() - started) * 1000.0
        return 0, {"error": str(e)}, latency_ms


def classify(http_status: int, body: dict[str, Any]) -> tuple[str, int]:
    if http_status == 0:
        return "down", EXIT_DOWN
    if http_status >= 500:
        return "down", EXIT_DOWN
    status = (body.get("status") or "").lower()
    if status == "ok":
        return "ok", EXIT_OK
    if status == "degraded":
        return "degraded", EXIT_DEGRADED
    if status == "down":
        return "down", EXIT_DOWN
    if 200 <= http_status < 300:
        return "ok", EXIT_OK
    return "degraded", EXIT_DEGRADED


def main() -> int:
    args = parse_args()
    status_code, body, latency_ms = probe(args.url, args.path, args.timeout)
    overall, exit_code = classify(status_code, body)
    record = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "url": args.url.rstrip("/") + args.path,
        "http_status": status_code,
        "overall": overall,
        "latency_ms": round(latency_ms, 2),
        "body": body,
    }
    print(json.dumps(record, default=str))
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
