#!/usr/bin/env python3
"""One-shot backfill: assign webhook_secret to any Bot row that lacks one.

Why
---
Bot.webhook_secret was added in Wave 2B. Existing rows on production DBs
will have ``NULL`` (or empty) values until this script runs. Each call:

  1. Adds the column to the live DB if it's missing (lightweight ALTER).
  2. Generates a fresh ``secrets.token_urlsafe(32)`` for every bot whose
     ``webhook_secret`` is NULL or empty.
  3. Reports how many rows were touched.

Idempotent — run as many times as you like; subsequent runs are no-ops.

Usage:
    cd backend
    source venv/bin/activate
    python scripts/migrate_webhook_secrets.py

For Postgres or other engines the column is added by SQLAlchemy schema
introspection via ``Base.metadata.create_all`` — but ``create_all`` only
adds tables, not columns. We therefore probe the column list and emit a
plain ``ALTER TABLE`` if needed.
"""
from __future__ import annotations

import os
import secrets
import sys

# Make sure the `app` package is importable when this script is invoked
# directly (e.g. `python scripts/migrate_webhook_secrets.py`).
HERE = os.path.dirname(os.path.abspath(__file__))
BACKEND = os.path.dirname(HERE)
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)

from sqlalchemy import inspect, text  # noqa: E402

from app.db.database import Base, SessionLocal, engine  # noqa: E402
from app.db.models import Bot  # noqa: E402  (ensures Bot is registered on Base)


def ensure_column_exists() -> bool:
    """Add bots.webhook_secret column if the live schema is missing it.

    Returns True iff the column was added.
    """
    insp = inspect(engine)
    if "bots" not in insp.get_table_names():
        # Brand-new DB — let create_all materialise everything below.
        return False
    cols = {c["name"] for c in insp.get_columns("bots")}
    if "webhook_secret" in cols:
        return False
    # SQLite + Postgres both accept this minimal ALTER. NOT NULL is enforced
    # at the application layer post-backfill.
    with engine.begin() as conn:
        conn.execute(text("ALTER TABLE bots ADD COLUMN webhook_secret VARCHAR(64)"))
    return True


def backfill() -> tuple[int, int]:
    """Populate empty webhook_secret values. Returns (scanned, updated)."""
    db = SessionLocal()
    try:
        bots = db.query(Bot).all()
        updated = 0
        for bot in bots:
            current = getattr(bot, "webhook_secret", None) or ""
            if not current:
                bot.webhook_secret = secrets.token_urlsafe(32)
                updated += 1
        if updated:
            db.commit()
        return len(bots), updated
    finally:
        db.close()


def main() -> int:
    # Make sure all tables exist (no-op on an existing DB).
    Base.metadata.create_all(bind=engine)
    added = ensure_column_exists()
    scanned, updated = backfill()
    print(
        "migrate_webhook_secrets: column_added={added} scanned={scanned} updated={updated}".format(
            added=added, scanned=scanned, updated=updated
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
