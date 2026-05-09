"""SQLAlchemy engine and session factory.

Supports SQLite (local/test) and Postgres (Koyeb + Neon prod). The
DATABASE_URL is read from settings; Postgres URLs are normalized to use
the psycopg2 driver and ``sslmode=require`` is appended if missing
(Neon requires SSL; some hosting providers don't auto-add it).
"""
from __future__ import annotations

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.engine.url import make_url
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from app.config import get_settings

settings = get_settings()


def _build_engine_url(raw_url: str) -> str:
    """Normalize the configured DATABASE_URL.

    - ``postgres://`` → ``postgresql+psycopg2://`` (Heroku-style URLs).
    - ``postgresql://`` (no driver) → ``postgresql+psycopg2://``.
    - For Postgres URLs without ``sslmode``, append ``sslmode=require``
      (Neon refuses non-SSL connections).
    - SQLite/other URLs are returned unchanged.
    """
    if raw_url.startswith("postgres://"):
        raw_url = "postgresql+psycopg2://" + raw_url[len("postgres://") :]
    elif raw_url.startswith("postgresql://") and "+psycopg2" not in raw_url.split("://", 1)[0]:
        raw_url = "postgresql+psycopg2://" + raw_url[len("postgresql://") :]

    if raw_url.startswith("postgresql"):
        url = make_url(raw_url)
        if "sslmode" not in url.query:
            url = url.update_query_dict({"sslmode": "require"})
        return url.render_as_string(hide_password=False)
    return raw_url


_engine_url = _build_engine_url(settings.DATABASE_URL)

if _engine_url.startswith("sqlite"):
    connect_args = {"check_same_thread": False}
    engine_kwargs: dict = {}
else:
    connect_args = {}
    # Reasonable defaults for a small free-tier deploy. Neon free pooler
    # caps at ~100 connections; Koyeb free runs a single instance, so a
    # tight pool keeps idle conns low.
    engine_kwargs = {
        "pool_size": 5,
        "max_overflow": 5,
        "pool_pre_ping": True,
        "pool_recycle": 1800,
    }

engine = create_engine(_engine_url, connect_args=connect_args, future=True, **engine_kwargs)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine, future=True)


# SQLite WAL mode — required to avoid "database is locked" errors under
# concurrent writes (the QuantRunner status update + the TradeLocker
# relay's account-state poll both write to strategy_state every few
# seconds). WAL allows concurrent readers and a single writer without
# blocking. Has no effect on Postgres.
if _engine_url.startswith("sqlite"):
    @event.listens_for(Engine, "connect")
    def _sqlite_pragmas(dbapi_conn, _connection_record):  # pragma: no cover
        cur = dbapi_conn.cursor()
        try:
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA synchronous=NORMAL")
            cur.execute("PRAGMA busy_timeout=5000")
        finally:
            cur.close()


class Base(DeclarativeBase):
    pass


def get_db():
    """FastAPI dependency that yields a SQLAlchemy session."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
