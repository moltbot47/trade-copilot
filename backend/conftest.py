"""Project-root conftest — bootstraps test DB and FastAPI TestClient.

Uses an in-memory SQLite shared across the engine via StaticPool so that
both the FastAPI dependency-overridden session and direct fixtures can
see the same schema/state.
"""
from __future__ import annotations

import os
import sys
from typing import Generator

# Ensure the ``app`` package is importable when pytest is invoked from the
# backend folder (where conftest.py lives). The project layout is:
#   backend/
#     app/
#     tests/
#     conftest.py  <-- this file
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Ensure tests never accidentally hit a real Postgres / network DB by
# overriding env BEFORE the app is imported.
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("ENCRYPTION_KEY", "test_only_fernet_key_32_bytes_xx")
os.environ.setdefault("SECRET_KEY", "test_only_secret_key_32_bytes_xx")
os.environ.setdefault("ENVIRONMENT", "development")

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool


@pytest.fixture()
def db_engine():
    """Fresh in-memory SQLite engine per-test with a StaticPool.

    StaticPool keeps the same underlying connection across SQLAlchemy
    Session objects, so the schema we create in this fixture is visible
    inside the FastAPI request handlers.
    """
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )
    # Import models lazily so DATABASE_URL env is already in place.
    from app.db.database import Base
    import app.db.models  # noqa: F401  (register models on Base)

    Base.metadata.create_all(bind=engine)
    yield engine
    Base.metadata.drop_all(bind=engine)
    engine.dispose()


@pytest.fixture()
def db_session(db_engine):
    """Yields a SQLAlchemy session bound to the per-test engine."""
    TestingSession = sessionmaker(
        autocommit=False, autoflush=False, bind=db_engine, future=True
    )
    sess = TestingSession()
    try:
        yield sess
    finally:
        sess.close()


@pytest.fixture()
def client(db_engine) -> Generator[TestClient, None, None]:
    """FastAPI TestClient with get_db overridden to use the test engine.

    The lifespan context is allowed to run because the override is safe
    (it just creates tables that already exist on our in-memory engine).
    Rate limiting is disabled in tests so multiple requests per test
    don't trip the 60/min default cap (and so that route-level limiters
    that require a Response param don't error in TestClient).
    """
    from app.core.rate_limit import limiter
    from app.db.database import get_db
    from app.main import app

    TestingSession = sessionmaker(
        autocommit=False, autoflush=False, bind=db_engine, future=True
    )

    def _override_get_db():
        s = TestingSession()
        try:
            yield s
        finally:
            s.close()

    app.dependency_overrides[get_db] = _override_get_db
    prev_enabled = limiter.enabled
    limiter.enabled = False
    # Use TestClient as a context manager so lifespan startup/shutdown runs.
    try:
        with TestClient(app) as c:
            yield c
    finally:
        limiter.enabled = prev_enabled
        app.dependency_overrides.clear()


@pytest.fixture()
def auth_email() -> str:
    """Standard test user email."""
    return "tester@example.com"


@pytest.fixture()
def auth_headers(auth_email: str, db_session) -> dict[str, str]:
    """Returns Bearer-token headers for the test user (auto-creates User row).

    Mirrors the prod auth flow: a signed JWT carrying {sub: email}. Routes
    accept the token via either the `tc_session` cookie or the
    Authorization header.
    """
    from app.api.users import get_or_create_user
    from app.core.jwt import issue_session_token

    get_or_create_user(db_session, auth_email)
    token, _ = issue_session_token(auth_email)
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture()
def seed_bots(db_session):
    """Seed the 3 starter bots + 1 latpfn bot for tests that need them."""
    from app.db.models import Bot, StrategyType

    # Fixed webhook_secret values for deterministic HMAC tests. Real seeds
    # use ``secrets.token_urlsafe(32)`` per-row.
    bots = [
        Bot(
            name="ORB Breakout",
            slug="orb-breakout",
            description="ORB",
            strategy_type=StrategyType.orb,
            backtest_win_rate=62.0,
            backtest_profit_factor=1.4,
            risk_level=3,
            instruments_csv="EURUSD",
            webhook_secret="test-secret-orb-breakout",
        ),
        Bot(
            name="Squeeze Momentum",
            slug="squeeze-momentum",
            description="Squeeze",
            strategy_type=StrategyType.squeeze,
            backtest_win_rate=58.0,
            backtest_profit_factor=1.6,
            risk_level=4,
            instruments_csv="EURUSD",
            webhook_secret="test-secret-squeeze-momentum",
        ),
        Bot(
            name="Stoch Hook Reversal",
            slug="stoch-hook-reversal",
            description="Stoch",
            strategy_type=StrategyType.stoch_hook,
            backtest_win_rate=65.0,
            backtest_profit_factor=1.3,
            risk_level=2,
            instruments_csv="XAUUSD",
            webhook_secret="test-secret-stoch-hook-reversal",
        ),
        Bot(
            name="LaT-PFN Momentum",
            slug="latpfn-momentum",
            description="ML momentum",
            strategy_type=StrategyType.latpfn_momentum,
            backtest_win_rate=61.0,
            backtest_profit_factor=1.5,
            risk_level=3,
            instruments_csv="BTCUSD,ETHUSD",
            webhook_secret="test-secret-latpfn-momentum",
        ),
        Bot(
            name="LaT-PFN Quant Trader",
            slug="latpfn-quant",
            description="Pyramiding LaT-PFN with active management",
            strategy_type=StrategyType.latpfn_quant,
            backtest_win_rate=63.0,
            backtest_profit_factor=1.7,
            risk_level=4,
            instruments_csv="BTCUSD",
            webhook_secret="test-secret-latpfn-quant",
        ),
    ]
    for b in bots:
        db_session.add(b)
    db_session.commit()
    for b in bots:
        db_session.refresh(b)
    return bots
