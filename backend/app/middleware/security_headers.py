"""Security headers middleware.

Adds: HSTS (production only), X-Content-Type-Options, Referrer-Policy,
Permissions-Policy, Content-Security-Policy (allows buymeacoffee.com).
"""
from __future__ import annotations

from typing import Awaitable, Callable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from app.config import get_settings

# Strict-ish CSP: allow self + BMC widget + inline-style/script for the BMC button.
# Note: BMC's button.prod.min.js is loaded from cdnjs.buymeacoffee.com; the API
# endpoint is buymeacoffee.com. We allow both. Inline scripts permitted because
# the BMC widget injects them; tighten via nonces if/when feasible.
_CSP = (
    "default-src 'self'; "
    "script-src 'self' 'unsafe-inline' https://cdnjs.buymeacoffee.com https://buymeacoffee.com; "
    "style-src 'self' 'unsafe-inline' https://cdnjs.buymeacoffee.com; "
    "img-src 'self' data: https://cdn.buymeacoffee.com https://img.buymeacoffee.com; "
    "font-src 'self' data:; "
    "connect-src 'self' https://buymeacoffee.com; "
    "frame-src https://buymeacoffee.com; "
    "frame-ancestors 'none'; "
    "base-uri 'self'; "
    "form-action 'self'"
)


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Append baseline security headers to every response."""

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        response = await call_next(request)
        is_prod = get_settings().ENVIRONMENT.lower() == "production"

        if is_prod:
            response.headers.setdefault(
                "Strict-Transport-Security",
                "max-age=63072000; includeSubDomains",
            )
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        response.headers.setdefault(
            "Permissions-Policy",
            "geolocation=(), camera=(), microphone=()",
        )
        response.headers.setdefault("Content-Security-Policy", _CSP)
        return response
