# ADR-0001 — FastAPI as the backend framework

- **Status**: Accepted
- **Date**: 2026-05-08

## Context

Trade Copilot's backend has three jobs: serve a small REST surface to the Next.js frontend, accept TradingView webhooks at sub-second latency, and run long-lived async strategy loops that talk to a broker and an inference service. We need a Python framework — the strategy code already depends on the Python ML ecosystem (LaT-PFN, NumPy, Pandas, scikit), and the maintainer is fluent in Python.

The candidates were FastAPI, Flask (with Quart for async), and Django REST Framework.

## Decision

We adopt **FastAPI 0.110+** with **Uvicorn** as the ASGI server, **Pydantic v2** for request/response schemas, and **SQLAlchemy 2.0 ORM** (sync mode) for persistence.

## Consequences

**Positive**
- Native async support — the signal router and the strategy runner can both await broker calls without thread pools.
- Pydantic v2 models double as DB schemas (`from_attributes=True`) and OpenAPI definitions, eliminating duplicated DTOs.
- OpenAPI spec is generated for free at `/docs`; we use it as the source of truth for `API.md`.
- The dependency-injection model (`Depends(get_db)`, `Depends(get_current_user)`) makes routers thin and testable.

**Negative**
- Mixing sync SQLAlchemy with async route handlers requires care — a long DB query in an async route blocks the event loop. Mitigated by keeping queries fast and short.
- FastAPI's middleware story is less mature than Django's; we leaned on `slowapi` for rate-limit and rolled CORS + global exception handler ourselves.

## Alternatives considered

| Option | Why rejected |
|--------|--------------|
| Flask + Quart | Mixing sync Flask routes with Quart async needs glue; team prefers a unified stack. |
| Django REST Framework | Heavier than needed; the admin and ORM features overlap with what we get from SQLAlchemy + a small CLI. |
| Node.js (Express / Hono) | Forces a Python ↔ Node split for the strategy code, which is itself Python-native. |

## Implementation notes

- Production process: `uvicorn app.main:app --host 0.0.0.0 --port 8000`.
- Routers are mounted under `/api`; `/health` is the only top-level route.
- The lifespan manager creates tables (`Base.metadata.create_all`) and seeds starter bots idempotently.
