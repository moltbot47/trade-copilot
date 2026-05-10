"""Alembic environment — wires migrations into the Trade Copilot app.

The Base.metadata is sourced from app.db.database; all ORM models are
imported (via app.db.models) so autogenerate can see every table.

DATABASE_URL is resolved at runtime from the same Settings the app uses
(so a one-off `alembic upgrade head` from CI or a Fly machine respects
.env / environment variables). The placeholder ``sqlalchemy.url`` in
alembic.ini is overridden here.
"""
from __future__ import annotations

import os
import sys
from logging.config import fileConfig
from pathlib import Path

from sqlalchemy import engine_from_config, pool

from alembic import context

# Make ``app`` importable when alembic is invoked from backend/.
_BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from app.db.database import Base  # noqa: E402
import app.db.models  # noqa: E402,F401  (register models on Base.metadata)

config = context.config

# Only run fileConfig when invoked from the CLI ($ alembic …). When the
# app's lifespan calls ``alembic.command.upgrade`` programmatically, the
# Python logging tree has already been configured by
# ``app.core.logging.configure_logging`` — re-running fileConfig there
# would clobber app loggers (and break pytest's caplog).
if (
    config.config_file_name is not None
    and os.getenv("ALEMBIC_SKIP_LOGGING") != "1"
    and sys.argv
    and Path(sys.argv[0]).name.startswith("alembic")
):
    fileConfig(config.config_file_name)


def _resolve_database_url() -> str:
    """Prefer explicit env var, fall back to app Settings (same .env)."""
    raw = os.getenv("DATABASE_URL")
    if raw:
        return raw
    from app.config import get_settings
    return get_settings().DATABASE_URL


# Override the placeholder URL from alembic.ini with the runtime value.
config.set_main_option("sqlalchemy.url", _resolve_database_url())

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode (emit SQL without a DBAPI)."""
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=url.startswith("sqlite") if url else False,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode against a live DB connection."""
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            # SQLite needs batch mode for ALTER TABLE — harmless on Postgres.
            render_as_batch=connection.dialect.name == "sqlite",
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
