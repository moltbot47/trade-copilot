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


def configure_logging(level: str | None = None) -> None:
    """Configure root + uvicorn + sqlalchemy loggers with JSONFormatter and redaction.

    Idempotent — safe to call multiple times.
    """
    log_level = (level or os.getenv("LOG_LEVEL", "INFO")).upper()

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JSONFormatter())
    handler.addFilter(SecretRedactionFilter())

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(log_level)

    # Make uvicorn/sqlalchemy use our handler too (don't double-emit).
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access", "sqlalchemy.engine"):
        lg = logging.getLogger(name)
        lg.handlers.clear()
        lg.propagate = True
        if name == "sqlalchemy.engine":
            lg.setLevel(os.getenv("SQLALCHEMY_LOG_LEVEL", "WARNING").upper())
        else:
            lg.setLevel(log_level)
