# Trade Copilot — Risk Matrix

This document enumerates the project's known risks across technical, security, regulatory, operational, and market categories. Each risk carries a probability score (Low=1, Medium=2, High=3), an impact score (Low=1, Medium=2, High=3), the product (1–9), the owning function, and a mitigation. This matrix is reviewed every two weeks and updated when a new risk is identified or an existing one materially changes.

**As-of date:** 2026-05-08.

---

## Scoring legend

| Score (P × I) | Treatment |
|---------------|-----------|
| 1–2 | Accept; revisit quarterly. |
| 3–4 | Mitigate when convenient. |
| 6 | Mitigate before next release. |
| 9 | Block release; mitigate now. |

---

## Risk register

| ID | Category | Risk | P | I | Score | Owner | Mitigation |
|----|----------|------|---|---|-------|-------|------------|
| R-01 | Regulatory | Regulators (SEC / state, CFTC) interpret the platform as offering investment advice or operating as an unregistered RIA / CTA. | 2 | 3 | **6** | Legal | Donation-only revenue (ADR-0003); no advice copy; mandatory `LEGAL.md`; "educational, not advisory" framing on every page; periodic counsel review. |
| R-02 | Security | TradeLocker access tokens leak — via DB exfiltration or `ENCRYPTION_KEY` compromise — letting an attacker place trades on a user's account. | 1 | 3 | **3** | Backend | Fernet encryption at rest (`app/core/crypto.py`); production-boot guard refuses placeholder keys; no plaintext credentials in logs (NFR-2); plan for key rotation runbook. |
| R-03 | Security | XSS in the dashboard exfiltrates the session cookie or replays requests as the user. | 1 | 3 | **3** | Frontend | HttpOnly + SameSite=Lax cookies (ADR-0004); React's default escaping; no `dangerouslySetInnerHTML`; CSP header (Wave 2). |
| R-04 | Operational | The auth router (`app/api/auth.py`) exists but is not mounted in `app/main.py`. Result: `/api/auth/login` returns 404 and the cookie flow is broken end-to-end. | 3 | 3 | **9** | Backend | ✅ **CLOSED 2026-05-09** — `auth.router` mounted in `main.py`; JWT-cookie auth shipped (ADR-0004); integration test in `test_jwt.py` exercises login → cookie → `/api/auth/me`; verified live in Fly logs (butler135@gmail.com session active). |
| R-05 | Technical | TradeLocker outage takes the product offline (read or write). | 2 | 2 | **4** | Backend | Graceful degradation in `/api/tradelocker/account` (NFR-3); `/health` is dependency-free; user-facing banner on the dashboard when last broker call failed (Wave 2). |
| R-06 | Technical | LaT-PFN model returns NaN, flat, or pathologically biased forecasts in low-volatility regimes, producing a string of losing trades. | 2 | 2 | **4** | Strategy | Confidence-threshold auto-tuner (`feedback.py`) tightens or pauses the runner; per-bot `max_drawdown_pct` cap; `paused_until` timeboxing. |
| R-07 | Operational | SQLite write contention on the strategy runner under sustained load (multiple users on the same bot/timeframe). | 2 | 2 | **4** | Backend | NFR-7 caps soak target at 100 users; document PostgreSQL migration trigger; `Settings.DATABASE_URL` is the only knob to flip. |
| R-08 | Security | Login enumeration via timing differences between "user exists" and "user does not exist" branches. | 2 | 1 | **2** | Backend | `get_or_create_user` always creates on miss → identical-shape responses; rate-limit 10/min/IP via slowapi; (Wave 2: constant-time email lookup). |
| R-09 | Regulatory | A user loses material funds and sues, alleging the platform was de facto giving advice. | 1 | 3 | **3** | Legal | `LEGAL.md` disclaimer; account never custodied (user's own broker); donation framing; arbitration clause in TOS (Wave 2 legal review). |
| R-10 | Market | Genesis FX changes its TradeLocker server name, T&Cs, or partner status — breaking the connect form. | 1 | 2 | **2** | Operations | Server name is a free-text user input (not hardcoded in the schema); helper text and error strings in two centralized places (`tradelocker.py`, connect page); switch ADR-0006 if needed. |
| R-11 | Operational | Frontend deploy on Vercel diverges from backend deploy on the Docker host — schema drift between Pydantic responses and TS types. | 2 | 2 | **4** | Full-stack | Generate TS types from FastAPI's OpenAPI on every CI run (Wave 2); hand-written client today is small enough to audit by eye. |
| R-12 | Technical | TradingView webhook is replayed by an attacker who guessed `bot_secret` (which is just `Bot.slug` — public). | 2 | 3 | **6** | Backend | ✅ **CLOSED 2026-05-08** — per-bot HMAC secret (auto-generated, 256-bit) verifies request signature with replay-protection via timestamp window; `Bot.webhook_secret` is rotatable via `POST /api/bots/{slug}/webhook/rotate` and never exposed in the public catalog; `app/core/webhook_signing.py` + `test_webhooks_hmac.py` cover the flow. Released in v0.2.0 (CHANGELOG). |
| R-13 | Security | Discord webhook URL leaks via env-dump in logs or stack trace. | 1 | 1 | **1** | Operations | Global exception handler returns generic 500; `DISCORD_WEBHOOK_URL` is read once at startup; never logged. |
| R-14 | Operational | Missing observability — outage detected by user complaint, not by alert. | 3 | 2 | **6** | Operations | Wave 1E adds health monitor + JSON logs; Wave 2 ships uptime-monitor SaaS integration (per project memory). |
| R-15 | Operational | Backup/restore for SQLite has not been validated end-to-end. | 2 | 3 | **6** | Operations | Cron `sqlite3 .backup` script + retention policy + restore-to-staging drill on each release. |
| R-16 | Market | Donations underperform expectations and the project becomes economically unviable. | 2 | 2 | **4** | Founder | Cost floor is low (single Docker host + Vercel free tier); BMC link is highly visible; Wave 2 considers software-license model if needed. |
| R-17 | Technical | A long-running strategy task accumulates a memory leak (e.g., growing TradeOutcome list) and OOM-kills the process. | 2 | 2 | **4** | Backend | Position monitor closes outcomes incrementally; runner uses bounded windows for snapshots; Docker restart-policy `unless-stopped`. |

---

## Top risks (score ≥ 6)

Last refreshed: 2026-05-11.

| Rank | Risk | Score | Status |
|------|------|-------|--------|
| 1 | R-01 — regulatory framing | 6 | Mitigated by ADR-0003 + LEGAL.md; recurring review. |
| 2 | R-14 — observability gap | 6 | Partially mitigated — Sentry + JSON logs + /metrics + reconciliation + daily summary shipped. External uptime SaaS (BetterUptime) still pending. |
| 3 | R-15 — backup unvalidated | 6 | SQLite `.backup` utility shipped (`scripts/`) with retention + integrity check. Restore-to-staging drill still pending. |

### Closed since previous refresh

| Risk | Resolution |
|---|---|
| R-04 — auth router not mounted (9) | ✅ JWT-cookie auth shipped (ADR-0004) · `auth.router` mounted in `main.py` · integration test in `test_jwt.py`. |
| R-12 — webhook replay (6) | ✅ Per-bot HMAC secret (rotatable) with timestamp replay-protection · v0.2.0. |

---

## Cross-References

- `REQUIREMENTS.md` — risks tie back to NFRs (1–10).
- `ARCHITECTURE.md` — quality attributes column "Tradeoff accepted" doubles as the architectural risk register.
- `adr/0003-donation-not-subscription-billing.md` — R-01 mitigation.
- `adr/0004-jwt-cookie-auth.md` — R-02 / R-03 mitigation.
