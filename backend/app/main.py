"""FastAPI entrypoint for Trade Copilot backend."""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware
from starlette.middleware.base import BaseHTTPMiddleware

from app.api import auth, bots, calculator, dashboard, subscriptions, tradelocker, users, webhooks
from app.config import get_settings
from app.core.rate_limit import limiter
from app.db.database import Base, SessionLocal, engine
from app.strategies import api as strategy_api

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


def _seed_starter_bots() -> None:
    """Idempotent seed of the 3 starter bots if missing."""
    from app.db.models import Bot, StrategyType  # local import to avoid circulars at boot

    starter = [
        dict(
            name="ORB Breakout",
            slug="orb-breakout",
            description="Opening Range Breakout - trades the first 30-min range break.",
            strategy_type=StrategyType.orb,
            backtest_win_rate=62.0,
            backtest_profit_factor=1.4,
            risk_level=3,
            instruments_csv="EURUSD,GBPUSD,XAUUSD",
        ),
        dict(
            name="Squeeze Momentum",
            slug="squeeze-momentum",
            description="Bollinger/Keltner squeeze release - rides volatility expansion.",
            strategy_type=StrategyType.squeeze,
            backtest_win_rate=58.0,
            backtest_profit_factor=1.6,
            risk_level=4,
            instruments_csv="EURUSD,USDJPY,GBPJPY",
        ),
        dict(
            name="Stoch Hook Reversal",
            slug="stoch-hook-reversal",
            description="Stochastic hook on overbought/oversold for mean-reversion entries.",
            strategy_type=StrategyType.stoch_hook,
            backtest_win_rate=65.0,
            backtest_profit_factor=1.3,
            risk_level=2,
            instruments_csv="XAUUSD,USDCAD",
        ),
    ]

    db = SessionLocal()
    try:
        for cfg in starter:
            if not db.query(Bot).filter(Bot.slug == cfg["slug"]).first():
                db.add(Bot(**cfg))
        db.commit()
    finally:
        db.close()


@asynccontextmanager
async def lifespan(_: FastAPI):
    get_settings().assert_production_safe()
    Base.metadata.create_all(bind=engine)
    try:
        _seed_starter_bots()
    except Exception as exc:  # never crash the app on seed failure
        logger.warning("seed_starter_bots failed: %s", exc)
    yield


settings = get_settings()
app = FastAPI(title="Trade Copilot API", version="0.1.0", lifespan=lifespan)

# --- Rate limiting ---
# slowapi expects the limiter on app.state and a 429 exception handler.
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
app.add_middleware(SlowAPIMiddleware)

# --- CORS (allow_credentials=True is required for cookie-based session) ---
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.allowed_origins_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# --- Security headers ---
class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        # Only meaningful over HTTPS — harmless to send on http and prevents
        # accidental http downgrades once the app is on TLS.
        if settings.cookie_secure:
            response.headers.setdefault(
                "Strict-Transport-Security",
                "max-age=31536000; includeSubDomains",
            )
        return response


app.add_middleware(SecurityHeadersMiddleware)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.exception_handler(Exception)
async def global_exception_handler(_, exc: Exception):
    logger.exception("unhandled: %s", exc)
    return JSONResponse(status_code=500, content={"detail": "internal_error"})


# Mount routers under /api
app.include_router(auth.router, prefix="/api")
app.include_router(webhooks.router, prefix="/api")
app.include_router(users.router, prefix="/api")
app.include_router(bots.router, prefix="/api")
app.include_router(subscriptions.router, prefix="/api")
app.include_router(tradelocker.router, prefix="/api")
app.include_router(dashboard.router, prefix="/api")
app.include_router(strategy_api.router, prefix="/api")
app.include_router(calculator.router, prefix="/api")
