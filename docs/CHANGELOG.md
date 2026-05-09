# Changelog

All notable changes to Trade Copilot are documented here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- `docs/STAKEHOLDERS.md` — sign-off matrix and change-management process
- `frontend/.eslintrc.json` — minimal Next.js eslint config so CI lint job is no longer soft-fail

### Fixed
- Unused `ExecutionStatus` import removed from `app/strategies/position_monitor.py` (CI ruff check now clean)

## [0.2.0] — 2026-05-08

### Added
- **HMAC-signed TradingView webhooks** — per-bot rotatable secret, replay-protected via timestamp window. Endpoints: `GET /api/bots/{slug}/webhook`, `POST /api/bots/{slug}/webhook/rotate`. Migration `scripts/migrate_webhook_secrets.py` backfills existing rows.
- **WCAG 2.1 AA compliance** — focus rings, skip-to-content link, prefers-reduced-motion, responsive nav, 44×44 touch targets, ARIA live regions, programmatic form errors, EmailGate focus trap. Color-contrast pairs documented at top of `globals.css`.
- **Observability stack** — structured JSON logs, request-logging middleware (X-Request-ID), `/health/detail` with DB + TradeLocker probes, `/metrics` Prometheus endpoint, Sentry integration (opt-in), frontend ErrorBoundary.
- **JWT-cookie auth** — replaces previous spoofable `X-User-Email` header. Endpoints: `POST /api/auth/login`, `POST /api/auth/logout`, `GET /api/auth/me`. HttpOnly + SameSite=Lax cookie, 30-day TTL.
- **Rate limiting** — `slowapi` with per-route limits (10/min login, 5/min tradelocker connect, 30/min calculator generate, 60/min default).
- **Test suite** — 180 backend tests (pytest) + 34 frontend tests (Vitest); 71% backend coverage with critical modules at 90%+.
- **Deployment infrastructure** — Dockerfile.backend (multi-stage), Dockerfile.frontend, docker-compose, GitHub Actions CI (lint + test + docker-build), Vercel + Railway configs, Makefile, DEPLOY.md runbook, root `.gitignore`, initial git commit.
- **Documentation** — REQUIREMENTS.md (18 FRs, 10 NFRs), USER_STORIES.md (15 stories), ARCHITECTURE.md (C4 diagrams), API.md (full endpoint reference), WCAG_AUDIT.md, RISK_MATRIX.md (17 risks), REQUIREMENTS_TRACEABILITY.md, 6 ADRs.
- **Compound calculator** — `/calculator` page with live preview + personalized PDF download.
- **LaT-PFN Momentum strategy** — runner, performance tracker, self-tuning feedback adjuster, mock inference (real GPU service deployable separately).
- **Production-safe startup guard** — refuses to boot in production with placeholder secrets.

### Changed
- `BotCard` and `Bot` type now use real backend field names (`backtest_*`, `instruments_csv`).
- TradeLocker account state schema expanded with `available_funds`, `open_pnl`, `today_net`, `positions_count`.
- Connect form: `server` and `env` separated; password show/hide toggle; live-money confirmation step; better error messages.
- Strategy `/status` endpoint returns 200 + empty state when bot not yet initialized (was 404).
- Backend security headers added: HSTS (production only), X-Content-Type-Options, Referrer-Policy.
- Field-name drift between backend and frontend strategy responses resolved with dual-name compatibility.

### Removed
- Legacy `X-User-Email` header trust path.

## [0.1.0] — 2026-05-08

### Added
- Initial scaffold: FastAPI backend, Next.js 14 frontend, SQLite storage, terminal/TUI design system, BMC donate button, Discord webhook relay.
- TradeLocker REST client tuned and verified against Genesis FX demo (D#2163244).
- BTC and ETH live round-trip orders verified on demo.
- Three Pine Script strategy templates.
