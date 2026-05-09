"""Tests for app.core.logging — JSONFormatter + secret redaction."""
from __future__ import annotations

import json
import logging

from app.core.logging import (
    JSONFormatter,
    SecretRedactionFilter,
    _redact_value,
    configure_logging,
)


def _make_record(
    msg: str = "hello",
    level: int = logging.INFO,
    extra: dict | None = None,
    name: str = "test.logger",
) -> logging.LogRecord:
    record = logging.LogRecord(
        name=name,
        level=level,
        pathname=__file__,
        lineno=1,
        msg=msg,
        args=(),
        exc_info=None,
    )
    if extra:
        for k, v in extra.items():
            record.__dict__[k] = v
    return record


def test_json_formatter_outputs_valid_json():
    record = _make_record("test message")
    out = JSONFormatter().format(record)
    parsed = json.loads(out)  # must not raise
    assert isinstance(parsed, dict)


def test_json_formatter_required_fields_present():
    record = _make_record("hi", level=logging.INFO, name="my.logger")
    parsed = json.loads(JSONFormatter().format(record))
    assert "ts" in parsed
    assert "level" in parsed
    assert "logger" in parsed
    assert "msg" in parsed
    assert parsed["level"] == "INFO"
    assert parsed["logger"] == "my.logger"
    assert parsed["msg"] == "hi"


def test_json_formatter_preserves_extra_fields():
    record = _make_record("with extras", extra={"trace_id": "abc123", "user_id": 7})
    parsed = json.loads(JSONFormatter().format(record))
    assert parsed["trace_id"] == "abc123"
    assert parsed["user_id"] == 7


def test_redact_value_redacts_password_keys():
    redacted = _redact_value("user_password", "hunter2")
    assert redacted == "***REDACTED***"


def test_redact_value_redacts_token_keys():
    assert _redact_value("api_token", "secret") == "***REDACTED***"
    assert _redact_value("auth_token", "x") == "***REDACTED***"
    assert _redact_value("apikey", "k") == "***REDACTED***"


def test_redact_value_redacts_authorization_keys():
    assert _redact_value("authorization", "Bearer xxx") == "***REDACTED***"


def test_redact_value_passes_through_safe_keys():
    assert _redact_value("user_id", 42) == 42
    assert _redact_value("trace_id", "abc") == "abc"
    assert _redact_value("status", 200) == 200


def test_redact_value_recurses_into_nested_dicts():
    nested = {"outer": {"password": "leak", "name": "ok"}}
    out = _redact_value("outer", nested)
    assert out["outer"]["password"] == "***REDACTED***"
    assert out["outer"]["name"] == "ok"


def test_secret_redaction_filter_redacts_extras():
    record = _make_record(
        "with secrets", extra={"password": "leak", "user_id": 7, "token": "tok123"}
    )
    SecretRedactionFilter().filter(record)
    assert record.password == "***REDACTED***"
    assert record.token == "***REDACTED***"
    assert record.user_id == 7


def test_configure_logging_idempotent():
    configure_logging()
    handlers_after_first = list(logging.getLogger().handlers)
    configure_logging()
    handlers_after_second = list(logging.getLogger().handlers)
    # configure_logging clears + re-adds, so count should be 1 each time.
    assert len(handlers_after_first) == 1
    assert len(handlers_after_second) == 1
