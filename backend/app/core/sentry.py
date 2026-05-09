"""Sentry init — graceful no-op if SENTRY_DSN unset or sentry_sdk missing.

Usage:
    from app.core.sentry import init_sentry
    init_sentry()  # call once at startup BEFORE creating FastAPI()
"""
from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)


def init_sentry() -> bool:
    """Initialize Sentry. Returns True if active, False otherwise."""
    dsn = os.getenv("SENTRY_DSN", "").strip()
    if not dsn:
        return False

    try:
        import sentry_sdk
        from sentry_sdk.integrations.fastapi import FastApiIntegration
        from sentry_sdk.integrations.starlette import StarletteIntegration
    except Exception as exc:  # pragma: no cover - sentry not installed
        logger.warning("sentry_sdk import failed; skipping init: %s", exc)
        return False

    try:
        sentry_sdk.init(
            dsn=dsn,
            release=os.getenv("APP_VERSION", "0.1.0"),
            environment=os.getenv("ENVIRONMENT", "development"),
            traces_sample_rate=float(os.getenv("SENTRY_TRACES_SAMPLE_RATE", "0.1")),
            send_default_pii=False,
            integrations=[
                StarletteIntegration(),
                FastApiIntegration(),
            ],
        )
        logger.info("sentry initialized", extra={"environment": os.getenv("ENVIRONMENT", "development")})
        return True
    except Exception as exc:
        logger.warning("sentry init failed: %s", exc)
        return False
