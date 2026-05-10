# Database Migrations

Trade Copilot uses [Alembic](https://alembic.sqlalchemy.org/) for schema
version tracking. A lightweight inline shim (`_apply_lightweight_migrations`
in `app/main.py`) is retained as a safety net for live databases that
haven't yet been stamped — both paths run at startup and are idempotent.

---

## Layout

```
backend/
├── alembic.ini               # Alembic config (script_location → migrations/)
└── migrations/
    ├── env.py                # Wires Base.metadata + app DATABASE_URL
    ├── script.py.mako        # Template for new revisions
    └── versions/
        └── 0001_baseline.py  # Empty baseline — current schema as of 2026-05-10
```

`Base.metadata` is sourced from `app.db.database`; every ORM model in
`app.db.models` is imported so `--autogenerate` sees the full schema.

---

## Common commands

All commands run from `backend/` with the venv activated:

```bash
cd backend && source venv/bin/activate
```

### Add a column / new table
```bash
alembic revision --autogenerate -m "add foo column to bar"
```
Review the generated file in `migrations/versions/` before committing —
autogenerate is helpful but not infallible (it misses CHECK constraint
changes, server defaults on some dialects, etc.).

### Apply pending migrations
```bash
alembic upgrade head
```
The app also runs this at startup via the FastAPI `lifespan` hook, so
fresh deploys are migrated automatically.

### Roll back one revision
```bash
alembic downgrade -1
```

### Show current revision
```bash
alembic current
```

### Show history
```bash
alembic history --verbose
```

---

## Production rollout

For an existing live database that predates Alembic (i.e. created by
`Base.metadata.create_all` + the inline shim):

1. Deploy the new code with Alembic wired in. On first boot, the
   `lifespan` will detect there's no `alembic_version` table and
   automatically run `alembic stamp head` — no manual intervention.
2. (Optional, but recommended) Verify with `alembic current` over a
   shell on the host:
   ```bash
   alembic current   # expect: 0001_baseline (head)
   ```

If you'd rather stamp manually before the new code rolls out:
```bash
DATABASE_URL="postgresql://…" alembic stamp head
```

---

## Alembic vs the inline shim

| Use Alembic when…                             | Use the inline shim when…                  |
| --------------------------------------------- | ------------------------------------------ |
| Adding a new table                            | The change is a low-risk additive column   |
| Renaming a table or column                    | …and the live DB is unlikely to be stamped |
| Adding/removing indexes or constraints        | (legacy migrations already in there)       |
| Anything Postgres-specific (ENUMs, JSONB ops) |                                            |
| Data migrations (UPDATE/DELETE in lockstep)   |                                            |

In general: **prefer Alembic** for all new schema work. The inline shim
exists for backward compatibility with deployments that ran before this
framework was added; new column adds in code should be paired with an
Alembic revision rather than a new entry in `_apply_lightweight_migrations`.

---

## How the boot sequence interacts

`backend/app/main.py::lifespan` runs, in order:

1. `Base.metadata.create_all(bind=engine)`
   — produces tables on a fresh DB (no-op if they exist).
2. `_run_alembic_upgrade()`
   — if no `alembic_version` row: `stamp head` (fresh DBs are already at head);
   — otherwise: `upgrade head` (apply any pending revisions).
3. `_apply_lightweight_migrations()`
   — additive ALTER TABLEs not yet captured in Alembic, plus one-off
     idempotent data fixes.

Failures in (2) and (3) are logged but never crash boot.

---

## Troubleshooting

**`alembic upgrade head` fails with "Target database is not up to date"**
— You probably created a revision without applying the previous one.
Run `alembic upgrade head` first, then re-run `revision --autogenerate`.

**Autogenerate produces an empty migration**
— Either the model change isn't visible to `target_metadata` (check that
the model is imported in `migrations/env.py` via `app.db.models`), or
Alembic can't detect the change (e.g. server default changes on SQLite).

**Pytest's `caplog` is empty after a test that triggers alembic**
— Already handled: `migrations/env.py` only calls `fileConfig` when
invoked as a CLI (`$ alembic …`). Programmatic calls (the lifespan path)
leave the app's logging tree untouched.
