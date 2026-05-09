# Trade Copilot — Requirements Specification

This document defines the functional, non-functional, and constraint requirements for Trade Copilot v0.1 (MVP). It is the canonical reference cited by every other artifact in the SDLC. See `USER_STORIES.md` for persona-driven acceptance criteria, `REQUIREMENTS_TRACEABILITY.md` for the closure matrix, and `ARCHITECTURE.md` for how each requirement maps to a system component.

---

## 1. Product Statement

Trade Copilot is an educational, donation-supported auto-trading dashboard. A user picks a backtested strategy "bot," connects their own Genesis FX (TradeLocker) account, dials in their risk aggression, and watches the trades execute on a live terminal-style dashboard. The system never custodies funds and never charges a subscription — donations flow through Buy Me a Coffee.

---

## 2. Functional Requirements

| ID | Requirement | Priority | Source |
|----|-------------|----------|--------|
| FR-1 | Users can authenticate via email-only login. The backend issues a signed JWT in an HttpOnly cookie (`tc_session`). Sessions last 30 days by default. | P0 | `app/api/auth.py` |
| FR-2 | First-time emails auto-create a User row; no password barrier in the MVP. | P0 | `app/api/users.py:get_or_create_user` |
| FR-3 | Users can browse the public bot catalog (name, slug, description, backtest win rate, profit factor, risk level, supported instruments). | P0 | `app/api/bots.py` |
| FR-4 | Users can subscribe to a bot, optionally specifying an aggression level (1–10, default 5). | P0 | `app/api/subscriptions.py` |
| FR-5 | Users can update aggression or pause a subscription, and can unsubscribe. | P0 | `app/api/subscriptions.py` |
| FR-6 | Users can connect a Genesis FX TradeLocker account (email, password, server `GENFX`, env `demo`/`live`). Credentials are encrypted at rest with Fernet (AES-128-CBC + HMAC). | P0 | `app/api/tradelocker.py`, `app/core/crypto.py` |
| FR-7 | Users can view their TradeLocker account state: balance, equity (projectedBalance), available funds, open PnL, today net, positions count. | P0 | `app/api/tradelocker.py:account_state` |
| FR-8 | The system accepts TradingView webhook signals at `/api/webhooks/tradingview`. Each signal carries `bot_secret`, instrument, side, optional entry/SL/TP, and base lot size. | P0 | `app/api/webhooks.py` |
| FR-9 | The signal router fans out received signals to every active subscriber, scaling lot size by aggression level. | P0 | `app/core/signal_router.py` |
| FR-10 | The risk engine refuses orders that would breach a user's `max_daily_loss_pct` (default 3%). | P0 | `app/core/risk_engine.py` |
| FR-11 | Users can start, stop, and inspect a LaT-PFN momentum strategy runner per `(bot_id, timeframe)` pair. | P1 | `app/strategies/api.py` |
| FR-12 | Users can view PnL summary, open positions, recent executions, equity curve, and feedback log on the dashboard. | P0 | `app/api/dashboard.py`, `app/strategies/api.py:equity` |
| FR-13 | The performance tracker computes rolling 20-trade win rate, profit factor, Sharpe, avg-R, max drawdown, and persists `PerformanceSnapshot` rows. | P1 | `app/strategies/performance_tracker.py` |
| FR-14 | The feedback adjuster mutates the strategy's confidence threshold (`tighten`/`loosen`/`pause`/`hold`) based on snapshot metrics. | P1 | `app/strategies/feedback.py` |
| FR-15 | An anonymous compound-growth calculator generates a personalized PDF and live preview JSON. | P1 | `app/api/calculator.py` |
| FR-16 | A liveness endpoint `GET /health` returns `{"status":"ok"}` without auth. | P0 | `app/main.py` |
| FR-17 | All received signals are broadcast to a Discord webhook (best-effort, never blocks the response). | P2 | `app/core/discord_notifier.py` |
| FR-18 | Donations are surfaced via a Buy Me a Coffee button on the homepage and in the layout. | P0 | `frontend/components/BMCButton.tsx` |

---

## 3. Non-Functional Requirements

| ID | Category | Requirement | Verification |
|----|----------|-------------|--------------|
| NFR-1 | Performance | Backend p95 latency under 500 ms for read endpoints (`/health`, `/api/bots`, `/api/dashboard/pnl`, `/api/auth/me`) under 50 RPS sustained. | Load test in CI; record p50/p95/p99. |
| NFR-2 | Security | TradeLocker email, access token, and refresh token are encrypted at rest with Fernet. No PII (email, account ID, raw credentials) in application logs. | Code review; grep audit on log lines; `crypto.py` unit tests. |
| NFR-3 | Availability | `/health` returns 200 even when TradeLocker is unreachable. Account/positions endpoints degrade gracefully (return `connected: true` with empty financials, not 500). | Chaos test: disable network egress to TradeLocker, hit dashboard. |
| NFR-4 | Accessibility | Every frontend page meets WCAG 2.1 Level AA: contrast ≥ 4.5:1 for body text, all controls keyboard-reachable, visible focus indicator, form labels and error association via `aria-describedby`. | `WCAG_AUDIT.md` checklist; axe-core scan in CI (Wave 2). |
| NFR-5 | Compliance | Product framing avoids RIA/CTA registration triggers: no advice, no managed accounts, no performance fees, donation-only monetization. `LEGAL.md` disclaims everything; the homepage states "Educational, not advisory." | Legal review of copy; `LEGAL.md` published. |
| NFR-6 | Auditability | Every order placement is logged with `user_id`, `signal_id`, ISO timestamps for receipt and execution attempt, and final `ExecutionStatus`. | Inspect `Execution` rows; structured log line per order. |
| NFR-7 | Scalability | The strategy runner can sustain 100 concurrent users on a 1m timeframe without dropping ticks (ingestion lag < 2s, p95 inference < 4s on Intel CPU). | Soak test: 100 mock users, 60-min run, measure tick-to-fanout latency. |
| NFR-8 | Browser support | Latest 2 versions of Chrome, Firefox, Safari (desktop). Mobile Safari/Chrome render the marketing pages but the dashboard is desktop-first. | BrowserStack or manual matrix per release. |
| NFR-9 | Deployability | Backend ships as a single Docker image; frontend ships as a Vercel-deployed Next.js app. CI runs lint + tests + Docker build on every PR. | `Dockerfile`, `docker-compose.yml`, GitHub Actions. |
| NFR-10 | Data retention | `Signal`, `Execution`, `TradeOutcome`, `PerformanceSnapshot` are retained indefinitely for the audit trail; user-deletable on request (GDPR Art. 17). | DPA + `DELETE /api/users/me` endpoint (Wave 2). |

---

## 4. Constraints

| ID | Constraint |
|----|------------|
| C-1 | Demo broker is **Genesis FX**, accessed via TradeLocker REST. Server name in the connect form is `GENFX`. |
| C-2 | The LaT-PFN forecasting model runs **CPU-only** on the user's Intel Mac, with 1.5–4 s inference per call. No GPU is required or assumed in production. |
| C-3 | Persistence is **SQLite** for the MVP (`./trade_copilot.db`). PostgreSQL is recommended once concurrent user count exceeds ~50. The DB URL is configured via `DATABASE_URL`. |
| C-4 | Donations only via **Buy Me a Coffee** at `buymeacoffee.com/dbutler`. No Stripe, no in-app payments, no subscriptions. See ADR-0003. |
| C-5 | Trade execution uses TradeLocker REST + WS (no MT5 bridge, no FIX). See ADR-0002. |
| C-6 | Backend is a single FastAPI process — no microservice split for the MVP. See ADR-0001. |
| C-7 | Auth is JWT in an HttpOnly cookie. No OAuth provider, no magic-link email, no SMS. See ADR-0004. |
| C-8 | Strategy "brain" for the LaT-PFN momentum bot is the open-source LaT-PFN model. See ADR-0005. |

---

## 5. Out of Scope (MVP)

- Mobile native apps (iOS/Android).
- Multi-broker support beyond Genesis FX.
- Backtesting UI inside the app (backtests live in `strategies/*.pine` for TradingView).
- Tax reporting / 1099 generation.
- Social copy-trading / leaderboards.
- Native push notifications (Discord webhook covers this for now).

---

## 6. Glossary

| Term | Meaning |
|------|---------|
| **Aggression level** | Integer 1–10 multiplier applied to the base lot size of incoming signals. |
| **Bot** | A registered strategy in the catalog. Each bot has a slug used as `bot_secret` in the TradingView webhook. |
| **Subscription** | A user's binding to a bot, with aggression and pause state. |
| **Signal** | A `TradingViewSignal` payload received via webhook. |
| **Execution** | A user-specific record of attempting to place an order in response to a signal. |
| **TradeOutcome** | A closed trade with realized PnL and R-multiple, used to feed the performance tracker. |
| **R-multiple** | Realized PnL divided by amount risked at entry. The currency-agnostic measure of trade quality. |
| **Confidence threshold** | The σ (sigma) units a LaT-PFN forecast must clear before the runner places a trade. Auto-tuned by the feedback adjuster. |

---

## 7. Cross-References

- `USER_STORIES.md` — every FR is bound to ≥ 1 user story with acceptance criteria.
- `ARCHITECTURE.md` — C4 diagrams showing where each FR lives.
- `RISK_MATRIX.md` — risks that threaten these requirements.
- `REQUIREMENTS_TRACEABILITY.md` — the FR → Story → Test → Source closure table.
- `API.md` — endpoint-level spec for FRs 1–18.
- `WCAG_AUDIT.md` — NFR-4 verification artifact.
- `adr/` — architecture decisions that lock in the constraints.
