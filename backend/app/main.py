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

from app.api import (
    auth,
    bots,
    calculator,
    dashboard,
    subscriptions,
    tradelocker,
    users,
    webhooks,
)
from app.api import health as health_api
from app.api import metrics as metrics_api
from app.config import get_settings
from app.core.logging import configure_logging
from app.core.rate_limit import limiter
from app.core.sentry import init_sentry
from app.db.database import Base, SessionLocal, engine
from app.middleware.request_logging import RequestLoggingMiddleware
from app.strategies import api as strategy_api
from app.ws.server import router as ws_router

# Configure structured logging + Sentry before app instantiation so
# startup messages flow through the JSON formatter.
configure_logging()
init_sentry()
logger = logging.getLogger(__name__)


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


def _seed_advanced_bots() -> None:
    """Idempotent seed of LaT-PFN momentum + quant bots."""
    import importlib
    for mod_name in ("seed_latpfn_bot", "seed_quant_bot"):
        try:
            # The seed scripts live at /app at runtime (see Dockerfile.backend)
            # and import app.* — they're safe to import-as-script here.
            mod = importlib.import_module(mod_name)
            if hasattr(mod, "main"):
                mod.main()
        except Exception as exc:  # never crash on seed
            logger.warning("seed %s failed: %s", mod_name, exc)


def _apply_lightweight_migrations() -> None:
    """Add columns to existing tables that SQLAlchemy's create_all skips.

    create_all only creates NEW tables — it won't ALTER an existing one
    to add a column we added in code. This keeps prod schemas in step
    without dragging in Alembic for trivial column adds. The pattern is
    inspect-first (works on both SQLite and Postgres) and the column
    definitions use ANSI-compatible types (FLOAT, default literal).
    """
    from sqlalchemy import inspect, text

    pending = [
        # (table_name, column_name, ansi_compatible_column_def)
        # FLOAT + DEFAULT <literal> are accepted by both SQLite and Postgres.
        ("cohorts", "max_favorable_r_seen", "FLOAT DEFAULT 0.0"),
        # Tiny-account live guardrails (added 2026-05-10 for $21 live experiment).
        ("users", "max_lot_override", "FLOAT DEFAULT NULL"),
        ("users", "daily_kill_switch_pct", "FLOAT DEFAULT NULL"),
        ("users", "max_concurrent_positions", "INTEGER DEFAULT NULL"),
        ("users", "exhaustion_filter_enabled", "BOOLEAN DEFAULT 0"),
    ]
    # StrategyTickLog auto-archive: keep only the most recent N rows per
    # (bot, timeframe). Cheap on every boot.
    try:
        from sqlalchemy import text as _t
        from app.db.models import StrategyTickLog  # ensure imported
        del StrategyTickLog
        _MAX_TICKS_PER_KEY = 10_000
        with engine.begin() as conn:
            # Compatible across SQLite and Postgres: delete rows whose id is
            # not in the latest N for their (bot_id, timeframe) bucket.
            conn.execute(_t(
                """
                DELETE FROM strategy_tick_log
                WHERE id IN (
                  SELECT id FROM (
                    SELECT id,
                           ROW_NUMBER() OVER (
                             PARTITION BY bot_id, timeframe
                             ORDER BY id DESC
                           ) AS rn
                    FROM strategy_tick_log
                  ) ranked
                  WHERE ranked.rn > :n
                )
                """
            ), {"n": _MAX_TICKS_PER_KEY})
    except Exception as exc:  # noqa: BLE001 — best-effort
        logger.warning("strategy_tick_log archive failed: %s", exc)
    insp = inspect(engine)
    with engine.begin() as conn:
        existing_tables = set(insp.get_table_names())
        for tbl, col, defn in pending:
            if tbl not in existing_tables:
                continue  # create_all will handle it
            cols = {c["name"] for c in insp.get_columns(tbl)}
            if col in cols:
                continue
            try:
                conn.execute(text(f"ALTER TABLE {tbl} ADD COLUMN {col} {defn}"))
                logger.info("migration: added %s.%s", tbl, col)
            except Exception as exc:  # never crash on migration
                logger.warning("migration %s.%s failed: %s", tbl, col, exc)

        # One-time data fixes (idempotent, run every boot but no-op if already
        # at desired state). Owner-requested override on 2026-05-10:
        # bump max_concurrent_positions from 1 → 3 for the live experiment
        # account. They've explicitly accepted the additional risk.
        try:
            res = conn.execute(
                text(
                    "UPDATE users SET max_concurrent_positions = :cap "
                    "WHERE email = :email AND (max_concurrent_positions IS NULL OR max_concurrent_positions < :cap)"
                ),
                {"cap": 3, "email": "butler135@gmail.com"},
            )
            if res.rowcount:
                logger.info(
                    "migration: bumped max_concurrent_positions to 3 for butler135@gmail.com"
                )
        except Exception as exc:
            logger.warning("max_concurrent_positions bump failed: %s", exc)


async def _periodic_token_refresh_task() -> None:
    """Background task: refresh all TradeLocker tokens every 6 hours.

    Removes the daily reconnect requirement that previously surfaced as
    401s on the runner. Failures (e.g. user has no refresh_token, or
    refresh endpoint rejects) are logged but do not raise — the user
    simply has to reconnect via the UI. Runs forever; cancelled on shutdown.
    """
    import asyncio as _aio
    from app.core.tradelocker_token_refresh import proactive_refresh_all

    interval_seconds = 6 * 3600
    while True:
        try:
            summary = await proactive_refresh_all()
            logger.info("periodic TL token refresh: %s", summary)
        except Exception as exc:
            logger.warning("periodic TL token refresh raised: %s", exc)
        try:
            await _aio.sleep(interval_seconds)
        except _aio.CancelledError:
            return


async def lifespan(_: FastAPI):
    import asyncio as _aio

    get_settings().assert_production_safe()
    Base.metadata.create_all(bind=engine)
    _apply_lightweight_migrations()
    try:
        _seed_starter_bots()
    except Exception as exc:  # never crash the app on seed failure
        logger.warning("seed_starter_bots failed: %s", exc)
    _seed_advanced_bots()

    refresh_task = _aio.create_task(_periodic_token_refresh_task())
    try:
        yield
    finally:
        refresh_task.cancel()
        try:
            await refresh_task
        except _aio.CancelledError:
            pass


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
# Request logging — last so we capture the full final status code.
app.add_middleware(RequestLoggingMiddleware)


@app.exception_handler(Exception)
async def global_exception_handler(_, exc: Exception):
    logger.exception("unhandled: %s", exc)
    return JSONResponse(status_code=500, content={"detail": "internal_error"})


# Operational endpoints (no /api prefix — standard for liveness/metrics)
app.include_router(health_api.router)   # /health, /health/detail
app.include_router(metrics_api.router)  # /metrics

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

# WebSocket endpoint — protocol uses /ws (no /api prefix per WS_PROTOCOL.md).
app.include_router(ws_router)
