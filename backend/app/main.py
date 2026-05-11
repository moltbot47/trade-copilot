"""FastAPI entrypoint for Trade Copilot backend."""
from __future__ import annotations

import logging

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
    mfa,
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
        ("users", "bot_paused", "BOOLEAN DEFAULT 0"),
        ("users", "mfa_secret", "TEXT DEFAULT NULL"),
        ("users", "mfa_enabled", "BOOLEAN DEFAULT 0"),
        ("users", "circuit_breaker_n_losses", "INTEGER DEFAULT NULL"),
        ("users", "circuit_breaker_cooldown_minutes", "INTEGER DEFAULT 60"),
        ("users", "circuit_breaker_tripped_at", "DATETIME DEFAULT NULL"),
        ("users", "failed_login_count", "INTEGER DEFAULT 0"),
        ("users", "locked_until", "DATETIME DEFAULT NULL"),
        ("users", "discord_webhook_url", "TEXT DEFAULT NULL"),
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

        # One-time cleanup: delete phantom trades from the synthetic-data
        # bug (pre-2026-05-10 fix). Synthetic BTC bars priced at ~$118
        # generated fake TradeOutcome rows that were never placed at the
        # broker. Using $1000 as the floor since a real BTC trade would
        # never be that low. Same for ETH (real >= $1000 in 2026).
        try:
            sql_clean_btc = text(
                "DELETE FROM trade_outcomes WHERE instrument = :sym AND entry_price < 1000"
            )
            sql_clean_eth = text(
                "DELETE FROM trade_outcomes WHERE instrument = :sym AND entry_price < 100"
            )
            r1 = conn.execute(sql_clean_btc, {"sym": "BTCUSD"})
            r2 = conn.execute(sql_clean_eth, {"sym": "ETHUSD"})
            sql_clean_cohort_rows = text(
                "DELETE FROM cohorts WHERE (instrument = 'BTCUSD' AND weighted_avg_entry < 1000) OR (instrument = 'ETHUSD' AND weighted_avg_entry < 100)"
            )
            try:
                r3 = conn.execute(sql_clean_cohort_rows)
            except Exception:
                r3 = None
            total = (r1.rowcount if r1 else 0) + (r2.rowcount if r2 else 0)
            cohort_total = (r3.rowcount if r3 else 0)
            if total or cohort_total:
                logger.info(
                    "migration: cleaned %d phantom trade_outcomes + %d cohorts (synthetic-data bug)",
                    total,
                    cohort_total,
                )
        except Exception as exc:
            logger.warning("phantom-trade cleanup failed: %s", exc)


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
            # Treat "all refreshes failed and we had at least one user" as
            # a health alert. Refreshed=0 with no users isn't a failure.
            try:
                from app.monitoring.alerts import maybe_alert
                had_users = (summary.get("refreshed", 0) + summary.get("failed", 0)) > 0
                all_failed = had_users and summary.get("refreshed", 0) == 0
                await maybe_alert("token_refresh", failure=all_failed)
            except Exception as exc:
                logger.debug("token-refresh alert publish failed: %s", exc)
        except Exception as exc:
            logger.warning("periodic TL token refresh raised: %s", exc)
            try:
                from app.monitoring.alerts import maybe_alert
                await maybe_alert("token_refresh", failure=True)
            except Exception:
                pass
        try:
            await _aio.sleep(interval_seconds)
        except _aio.CancelledError:
            return


async def _auto_resume_runners() -> None:
    """On startup, re-spawn runners that were running when the process died.

    StrategyState.is_running is set when the runner starts and cleared
    when it stops cleanly. After a machine restart (Fly deploy, crash,
    OOM kill), the DB row still says is_running=True but the asyncio
    task is gone — so the bot is silently dead. This restores them.

    Skips:
      - states with paused_until > now (cooldown)
      - bots with no user subscriptions (nobody to trade for)
      - rows missing required fields
    """
    from datetime import datetime as _dt
    from app.db.database import SessionLocal
    from app.db.models import StrategyState, Bot, Subscription, User

    db = SessionLocal()
    try:
        states = db.query(StrategyState).filter(StrategyState.is_running.is_(True)).all()
    finally:
        db.close()

    if not states:
        return

    for state in states:
        try:
            if state.paused_until and state.paused_until > _dt.utcnow():
                logger.info("auto-resume skip: bot=%s tf=%s paused until %s",
                            state.bot_id, state.timeframe, state.paused_until)
                continue
            db = SessionLocal()
            try:
                bot = db.query(Bot).filter(Bot.id == state.bot_id).first()
                if bot is None:
                    continue
                subs = (
                    db.query(Subscription, User)
                    .join(User, Subscription.user_id == User.id)
                    .filter(
                        Subscription.bot_id == state.bot_id,
                        Subscription.is_paused.is_(False),
                        User.is_active.is_(True),
                    )
                    .all()
                )
                user_emails = [u.email for _, u in subs]
                if not user_emails:
                    continue
                symbols = [s.strip() for s in (bot.instruments_csv or "").split(",") if s.strip()]
                if not symbols:
                    continue
            finally:
                db.close()

            # Pick runner type by bot strategy. Match the same logic as
            # /api/strategy/start so resume behaves identically.
            from app.db.models import StrategyType
            from app.db.database import SessionLocal as _SL

            if bot.strategy_type in (StrategyType.latpfn_quant, StrategyType.latpfn_momentum):
                from app.strategies.runner import QuantRunner
                await QuantRunner.start(
                    db_session_factory=_SL,
                    bot_id=state.bot_id,
                    timeframe=state.timeframe,
                    symbols=symbols,
                    user_emails=user_emails,
                )
            else:
                from app.strategies.runner import StrategyRunner
                await StrategyRunner.start(
                    db_session_factory=_SL,
                    bot_id=state.bot_id,
                    timeframe=state.timeframe,
                    symbols=symbols,
                    user_emails=user_emails,
                )
            logger.info("auto-resumed runner bot=%s tf=%s users=%d symbols=%s",
                        state.bot_id, state.timeframe, len(user_emails), symbols)
        except Exception as exc:
            logger.warning("auto-resume failed bot=%s tf=%s: %s",
                           state.bot_id, state.timeframe, exc)


def _run_alembic_upgrade() -> None:
    """Apply Alembic migrations to ``head`` at startup.

    On a fresh database, ``Base.metadata.create_all`` has already produced
    the schema, so the only thing left for Alembic to do is stamp the
    baseline revision (``0001_baseline`` is a no-op). On an existing DB
    that's been stamped previously, this applies any pending revisions.
    Failures are logged but never crash boot — ``_apply_lightweight_migrations``
    remains the belt-and-suspenders for live DBs that haven't been stamped yet.
    """
    import os
    from pathlib import Path

    try:
        from alembic import command
        from alembic.config import Config
        from alembic.runtime.migration import MigrationContext
    except ImportError as exc:
        logger.warning("alembic not installed; skipping migrations: %s", exc)
        return

    ini_path = Path(__file__).resolve().parent.parent / "alembic.ini"
    if not ini_path.exists():
        logger.warning("alembic.ini not found at %s; skipping migrations", ini_path)
        return

    cfg = Config(str(ini_path))
    # Ensure alembic uses the same DATABASE_URL as the running app.
    db_url = os.getenv("DATABASE_URL") or get_settings().DATABASE_URL
    cfg.set_main_option("sqlalchemy.url", db_url)

    try:
        with engine.connect() as conn:
            mig_ctx = MigrationContext.configure(conn)
            current_rev = mig_ctx.get_current_revision()
        if current_rev is None:
            # Fresh DB (or pre-Alembic live DB): create_all already produced
            # the tables, so stamp head rather than re-running migrations.
            command.stamp(cfg, "head")
            logger.info("alembic: stamped fresh DB at head")
        else:
            command.upgrade(cfg, "head")
            logger.info("alembic: upgraded from %s to head", current_rev)
    except Exception as exc:  # noqa: BLE001 — never crash boot on migrations
        logger.warning("alembic upgrade failed (continuing): %s", exc)


async def lifespan(_: FastAPI):
    import asyncio as _aio

    get_settings().assert_production_safe()
    Base.metadata.create_all(bind=engine)
    _run_alembic_upgrade()
    _apply_lightweight_migrations()
    try:
        _seed_starter_bots()
    except Exception as exc:  # never crash the app on seed failure
        logger.warning("seed_starter_bots failed: %s", exc)
    _seed_advanced_bots()

    refresh_task = _aio.create_task(_periodic_token_refresh_task())

    # Position reconciliation — periodic DB↔broker drift detection.
    from app.strategies.reconciliation import periodic_reconciliation_task
    reconcile_task = _aio.create_task(periodic_reconciliation_task())

    # Daily summary — posts to Discord at 00:00 UTC.
    from app.monitoring.daily_summary import daily_summary_task
    summary_task = _aio.create_task(daily_summary_task())

    # Auto-resume runners that were running before the last shutdown.
    try:
        await _auto_resume_runners()
    except Exception as exc:
        logger.warning("auto-resume runners failed: %s", exc)
    try:
        yield
    finally:
        for task in (refresh_task, reconcile_task, summary_task):
            task.cancel()
            try:
                await task
            except _aio.CancelledError:
                pass


settings = get_settings()
app = FastAPI(title="Trade Copilot API", version="0.1.0", lifespan=lifespan)

# --- Rate limiting ---
# slowapi expects the limiter on app.state and a 429 exception handler.
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)  # type: ignore[arg-type]
app.add_middleware(SlowAPIMiddleware)

# --- CORS (allow_credentials=True is required for cookie-based session) ---
# We allow the canonical apex (production) AND any *.jetlag-recovery.com
# subdomain (www, staging, future) via regex. Without the regex, a user
# arriving at https://www.trading.jetlag-recovery.com or a Vercel preview
# would silently fail the preflight and the frontend would display the
# generic "server unavailable" error.
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.allowed_origins_list,
    allow_origin_regex=r"https://([a-z0-9-]+\.)*jetlag-recovery\.com",
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
app.include_router(metrics_api.latency_router, prefix="/api")  # /api/metrics/latency

# Mount routers under /api
app.include_router(auth.router, prefix="/api")
app.include_router(mfa.router, prefix="/api")
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
