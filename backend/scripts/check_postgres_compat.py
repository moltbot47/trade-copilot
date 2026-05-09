#!/usr/bin/env python
"""Pre-deploy Postgres / SQLite compatibility check.

Run this against whatever ``DATABASE_URL`` is currently set to verify
the app can:
  1. Connect.
  2. Run ``Base.metadata.create_all`` (i.e. the ORM types are dialect-safe).
  3. Apply the lightweight ALTER TABLE migrations.
  4. List the resulting tables.

Useful before pointing Koyeb at a fresh Neon instance — catches any
schema issues *before* the lifespan boot sequence on a cold deploy.

Usage:
    cd backend && source venv/bin/activate
    DATABASE_URL='postgresql+psycopg2://USER:PASS@host/db?sslmode=require' \
        python scripts/check_postgres_compat.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

# Allow running from repo root or backend/.
HERE = Path(__file__).resolve().parent
BACKEND_ROOT = HERE.parent
sys.path.insert(0, str(BACKEND_ROOT))


def main() -> int:
    from sqlalchemy import inspect, text

    # Importing app.* loads settings + engine using DATABASE_URL.
    from app.config import get_settings
    from app.db.database import Base, engine
    import app.db.models  # noqa: F401  (register all models on Base)
    from app.main import _apply_lightweight_migrations

    settings = get_settings()
    # Hide the password when logging.
    safe_url = engine.url.render_as_string(hide_password=True)
    print(f"[check] dialect={engine.dialect.name}  url={safe_url}")
    print(f"[check] ENVIRONMENT={settings.ENVIRONMENT}")

    print("[check] connecting + SELECT 1 ...")
    with engine.connect() as conn:
        result = conn.execute(text("SELECT 1")).scalar()
        assert result == 1, f"unexpected SELECT 1 result: {result!r}"
    print("[check]   ok")

    print("[check] Base.metadata.create_all ...")
    Base.metadata.create_all(bind=engine)
    print("[check]   ok")

    print("[check] _apply_lightweight_migrations ...")
    _apply_lightweight_migrations()
    print("[check]   ok")

    print("[check] tables present:")
    insp = inspect(engine)
    tables = sorted(insp.get_table_names())
    for t in tables:
        cols = [c["name"] for c in insp.get_columns(t)]
        print(f"  - {t}  ({len(cols)} cols)")
    print(f"[check] total tables: {len(tables)}")

    print("[check] all good. safe to deploy.")
    return 0


if __name__ == "__main__":
    # Exit 1 on any uncaught error so CI / shell can short-circuit.
    try:
        sys.exit(main())
    except Exception as exc:  # noqa: BLE001
        print(f"[check] FAILED: {type(exc).__name__}: {exc}", file=sys.stderr)
        # Suppress secrets from any psycopg2 connect error
        sys.exit(1)
