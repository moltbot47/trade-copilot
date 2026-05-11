"""Structured JSON logging for Trade Copilot.

Usage:
    from app.core.logging import configure_logging
    configure_logging()  # call once at startup (e.g. inside lifespan or before app=FastAPI())

Honors LOG_LEVEL env var (default INFO). Redacts secrets (password, token, secret,
key, authorization) from log records before emit.
"""
from __future__ import annotations

import json
import logging
import os
import re
import sys
from datetime import datetime, timezone
from typing import Any

# Keys that suggest a secret value — case-insensitive substring match.
_SECRET_KEY_PATTERN = re.compile(r"(password|token|secret|api_key|apikey|authorization)", re.IGNORECASE)
_REDACTED = "***REDACTED***"

# Fields the logging library always sets; we don't want to clobber them in extra.
_LOGRECORD_RESERVED = {
    "name", "msg", "args", "levelname", "levelno", "pathname", "filename",
    "module", "exc_info", "exc_text", "stack_info", "lineno", "funcName",
    "created", "msecs", "relativeCreated", "thread", "threadName",
    "processName", "process", "message", "asctime", "taskName",
}


def _redact_value(key: str, value: Any) -> Any:
    """If the key looks like a secret, replace value with REDACTED. Recurse into dicts."""
    if isinstance(value, dict):
        return {k: _redact_value(k, v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_redact_value(key, v) for v in value]
    if _SECRET_KEY_PATTERN.search(key):
        return _REDACTED
    return value


class SecretRedactionFilter(logging.Filter):
    """Strips secret-looking values from log record extras."""

    def filter(self, record: logging.LogRecord) -> bool:
        for attr, val in list(record.__dict__.items()):
            if attr in _LOGRECORD_RESERVED:
                continue
            try:
                record.__dict__[attr] = _redact_value(attr, val)
            except Exception:
                # Never let redaction crash logging.
                pass
        return True


class JSONFormatter(logging.Formatter):
    """Emits {ts, level, logger, msg, ...extra_fields} as a single JSON line."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        if record.stack_info:
            payload["stack_info"] = self.formatStack(record.stack_info)

        for attr, val in record.__dict__.items():
            if attr in _LOGRECORD_RESERVED or attr.startswith("_"):
                continue
            try:
                json.dumps(val, default=str)
                payload[attr] = val
            except (TypeError, ValueError):
                payload[attr] = str(val)

        return json.dumps(payload, default=str)


class AxiomHandler(logging.Handler):
    """Ship JSON log records to Axiom via their ingest HTTP endpoint.

    Best-effort and silent on failure — never lets a logging crash bubble
    up. Batched + flushed every ~2s by a background thread so logging
    stays non-blocking. Disabled unless both AXIOM_API_TOKEN and
    AXIOM_DATASET env vars are present.

    Docs: https://axiom.co/docs/send-data/http
    """
    def __init__(self, token: str, dataset: str, *, batch_size: int = 50,
                 flush_interval: float = 2.0) -> None:
        super().__init__()
        self.url = f"https://api.axiom.co/v1/datasets/{dataset}/ingest"
        self.headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }
        self.batch_size = batch_size
        self.flush_interval = flush_interval
        import queue as _q
        self._q: "_q.Queue[dict]" = _q.Queue(maxsize=10000)
        import threading
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._worker, name="axiom-log-shipper", daemon=True
        )
        self._thread.start()

    def emit(self, record: logging.LogRecord) -> None:
        try:
            # The JSONFormatter is set on a sibling handler; serialize the
            # raw record fields ourselves here to keep the two handlers
            # independent.
            entry = {
                "_time": record.created,
                "level": record.levelname,
                "logger": record.name,
                "msg": record.getMessage(),
            }
            for k, v in getattr(record, "__dict__", {}).items():
                if k.startswith("_") or k in ("args", "msg", "msecs", "relativeCreated"):
                    continue
                if isinstance(v, (str, int, float, bool, type(None))):
                    entry[k] = v
            # Non-blocking — drop on overflow rather than backing up the loop.
            self._q.put_nowait(entry)
        except Exception:  # noqa: BLE001
            pass

    def _worker(self) -> None:
        import json
        import time
        import urllib.request

        batch: list[dict] = []
        last_flush = time.monotonic()
        while not self._stop.is_set():
            timeout = max(0.05, self.flush_interval - (time.monotonic() - last_flush))
            try:
                entry = self._q.get(timeout=timeout)
                batch.append(entry)
            except Exception:  # noqa: BLE001 — Empty
                pass
            if (
                batch and (
                    len(batch) >= self.batch_size
                    or time.monotonic() - last_flush >= self.flush_interval
                )
            ):
                try:
                    req = urllib.request.Request(
                        self.url,
                        data=json.dumps(batch).encode(),
                        headers=self.headers,
                    )
                    urllib.request.urlopen(req, timeout=5)
                except Exception:  # noqa: BLE001 — never crash on log shipping
                    pass
                batch = []
                last_flush = time.monotonic()


def configure_logging(level: str | None = None) -> None:
    """Configure root + uvicorn + sqlalchemy loggers with JSONFormatter and redaction.

    Idempotent — safe to call multiple times.

    Optional Axiom shipping is enabled when both ``AXIOM_API_TOKEN`` and
    ``AXIOM_DATASET`` are set. The handler is fire-and-forget — log
    record processing never blocks on the HTTP send.
    """
    log_level = (level or os.getenv("LOG_LEVEL") or "INFO").upper()

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JSONFormatter())
    handler.addFilter(SecretRedactionFilter())

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(log_level)

    # Optional Axiom shipper — runs in parallel with stdout, never replaces it.
    axiom_token = os.getenv("AXIOM_API_TOKEN", "").strip()
    axiom_dataset = os.getenv("AXIOM_DATASET", "").strip()
    if axiom_token and axiom_dataset:
        try:
            axiom_handler = AxiomHandler(axiom_token, axiom_dataset)
            axiom_handler.addFilter(SecretRedactionFilter())
            root.addHandler(axiom_handler)
        except Exception:  # noqa: BLE001 — never block boot on logging setup
            pass

    # Make uvicorn/sqlalchemy use our handler too (don't double-emit).
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access", "sqlalchemy.engine"):
        lg = logging.getLogger(name)
        lg.handlers.clear()
        lg.propagate = True
        if name == "sqlalchemy.engine":
            lg.setLevel(os.getenv("SQLALCHEMY_LOG_LEVEL", "WARNING").upper())
        else:
            lg.setLevel(log_level)
