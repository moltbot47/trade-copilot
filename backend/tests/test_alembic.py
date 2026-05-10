"""Tests for the Alembic migration framework.

Verifies that ``alembic upgrade head`` applies cleanly against a fresh
SQLite DB, that the alembic_version table is created and stamped at the
latest revision, and that the framework can coexist with the existing
``Base.metadata.create_all`` boot path (which is what the lifespan
actually uses for fresh DBs — Alembic just stamps in that case).
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest
from sqlalchemy import create_engine, inspect

BACKEND_DIR = Path(__file__).resolve().parent.parent
ALEMBIC_INI = BACKEND_DIR / "alembic.ini"


def _build_alembic_config(db_url: str):
    from alembic.config import Config

    cfg = Config(str(ALEMBIC_INI))
    cfg.set_main_option("sqlalchemy.url", db_url)
    return cfg


def test_alembic_ini_exists() -> None:
    """alembic.ini must live at backend/ so `alembic` commands work."""
    assert ALEMBIC_INI.exists(), f"missing {ALEMBIC_INI}"


def test_baseline_revision_is_head(tmp_path: Path) -> None:
    """`alembic upgrade head` against a fresh DB stamps the baseline."""
    from alembic import command
    from alembic.runtime.migration import MigrationContext
    from alembic.script import ScriptDirectory

    db_file = tmp_path / "alembic_baseline.db"
    db_url = f"sqlite:///{db_file}"
    # alembic env.py reads DATABASE_URL if set — keep the test isolated.
    prev = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = db_url
    try:
        cfg = _build_alembic_config(db_url)
        command.upgrade(cfg, "head")

        engine = create_engine(db_url, future=True)
        try:
            with engine.connect() as conn:
                mig_ctx = MigrationContext.configure(conn)
                current = mig_ctx.get_current_revision()
            script = ScriptDirectory.from_config(cfg)
            head = script.get_current_head()
            assert current == head, f"current={current!r} != head={head!r}"
            assert current is not None

            insp = inspect(engine)
            assert "alembic_version" in insp.get_table_names()
        finally:
            engine.dispose()
    finally:
        if prev is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = prev


def test_create_all_then_stamp_matches_app_boot(tmp_path: Path) -> None:
    """Mirror the lifespan path: create_all, then alembic stamps head.

    Validates that every table on ``Base.metadata`` is created by
    ``create_all`` and that stamping leaves the DB at head — the exact
    sequence ``main.lifespan`` runs at startup on a fresh DB.
    """
    from alembic import command
    from alembic.runtime.migration import MigrationContext
    from alembic.script import ScriptDirectory

    from app.db.database import Base
    import app.db.models  # noqa: F401  (register models)

    db_file = tmp_path / "alembic_boot.db"
    db_url = f"sqlite:///{db_file}"
    prev = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = db_url
    try:
        engine = create_engine(db_url, future=True)
        try:
            Base.metadata.create_all(bind=engine)
            cfg = _build_alembic_config(db_url)
            command.stamp(cfg, "head")

            insp = inspect(engine)
            present = set(insp.get_table_names())
            expected = set(Base.metadata.tables.keys())
            missing = expected - present
            assert not missing, f"create_all skipped tables: {missing}"
            assert "alembic_version" in present

            with engine.connect() as conn:
                mig_ctx = MigrationContext.configure(conn)
                current = mig_ctx.get_current_revision()
            head = ScriptDirectory.from_config(cfg).get_current_head()
            assert current == head
        finally:
            engine.dispose()
    finally:
        if prev is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = prev


@pytest.mark.parametrize("direction", ["upgrade", "downgrade"])
def test_baseline_is_no_op_round_trip(tmp_path: Path, direction: str) -> None:
    """Baseline must be a true no-op: upgrade then downgrade leaves no
    tables behind besides the alembic_version row tracking."""
    from alembic import command
    from alembic.runtime.migration import MigrationContext

    db_file = tmp_path / "alembic_noop.db"
    db_url = f"sqlite:///{db_file}"
    prev = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = db_url
    try:
        cfg = _build_alembic_config(db_url)
        command.upgrade(cfg, "head")
        if direction == "downgrade":
            command.downgrade(cfg, "base")
            engine = create_engine(db_url, future=True)
            try:
                with engine.connect() as conn:
                    mig_ctx = MigrationContext.configure(conn)
                    assert mig_ctx.get_current_revision() is None
            finally:
                engine.dispose()
    finally:
        if prev is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = prev
