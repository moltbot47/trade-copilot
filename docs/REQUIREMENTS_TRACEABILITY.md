# Trade Copilot — Requirements Traceability Matrix

This file closes the requirements loop: every functional requirement has an owning user story, an automated test, a source file, and a status. It is the single artifact a reviewer can consult to verify "is it built, is it tested, is it shipped." Update on every PR that touches an FR-bearing surface.

**As-of date:** 2026-05-08.

**Status legend:** `Implemented` (code exists), `Tested` (covered by automated test), `Verified` (manually verified end-to-end on staging), `Open` (gap).

---

## Matrix

| FR / NFR | User Story | Test File | Source File | Status |
|----------|------------|-----------|-------------|--------|
| FR-1 | US-2 | `backend/tests/test_auth.py::test_login_sets_cookie` | `app/api/auth.py`, `app/core/jwt.py` | Implemented; **router not mounted** (R-04) — pending fix before Verified |
| FR-2 | US-2 | `backend/tests/test_auth.py::test_first_email_creates_user` | `app/api/users.py:get_or_create_user` | Implemented |
| FR-3 | US-1 | `backend/tests/test_bots.py::test_list_bots` | `app/api/bots.py` | Implemented; Tested in Wave 1B |
| FR-4 | US-3 | `backend/tests/test_subscriptions.py::test_create_subscription` | `app/api/subscriptions.py` | Implemented; Tested |
| FR-5 | US-4 | `backend/tests/test_subscriptions.py::test_patch_pause`, `::test_delete_other_user_returns_404` | `app/api/subscriptions.py` | Implemented; Tested |
| FR-6 | US-5 | `backend/tests/test_tradelocker.py::test_connect_encrypts_creds` | `app/api/tradelocker.py`, `app/core/crypto.py`, `app/core/tradelocker_client.py` | Implemented; Tested with fixture (`tradelocker_config.json`) |
| FR-7 | US-6 | `backend/tests/test_tradelocker.py::test_account_state_graceful_degrade` | `app/api/tradelocker.py:account_state` | Implemented |
| FR-8 | US-7 | `backend/tests/test_webhooks.py::test_tradingview_persists_signal` | `app/api/webhooks.py` | Implemented; Tested |
| FR-9 | US-7 | `backend/tests/test_signal_router.py::test_fan_out_creates_executions` | `app/core/signal_router.py` | Implemented |
| FR-10 | US-8 | `backend/tests/test_risk_engine.py::test_max_daily_loss_blocks_order` | `app/core/risk_engine.py` | Implemented |
| FR-11 | US-9 | `backend/tests/test_strategy_api.py::test_start_stop_status` | `app/strategies/api.py`, `app/strategies/runner.py` | Implemented |
| FR-12 | US-10 | `backend/tests/test_dashboard.py::test_pnl_summary`, `test_strategy_api.py::test_equity_curve` | `app/api/dashboard.py`, `app/strategies/api.py:equity` | Implemented |
| FR-13 | US-10, US-11 | `backend/tests/test_performance_tracker.py::test_snapshot_after_20_trades` | `app/strategies/performance_tracker.py` | Implemented |
| FR-14 | US-11 | `backend/tests/test_feedback.py::test_tighten_on_low_winrate`, `::test_pause_on_drawdown` | `app/strategies/feedback.py` | Implemented |
| FR-15 | US-12 | `backend/tests/test_calculator.py::test_preview_math`, `::test_pdf_streams` | `app/api/calculator.py`, `app/core/compound_pdf.py` | Implemented; Tested |
| FR-16 | US-14 | `backend/tests/test_health.py::test_health_returns_ok` | `app/main.py` | Implemented; Tested |
| FR-17 | US-7 | `backend/tests/test_webhooks.py::test_discord_failure_does_not_break_response` | `app/core/discord_notifier.py` | Implemented |
| FR-18 | US-13 | Frontend Playwright: `bmc-button.spec.ts::renders-on-home` | `frontend/components/BMCButton.tsx` | Implemented; Wave 2 adds Playwright |

---

## Non-functional traceability

| NFR | User Story | Test File | Source / Artifact | Status |
|-----|------------|-----------|-------------------|--------|
| NFR-1 (perf p95 < 500 ms) | (cross-cutting) | `backend/tests/perf/test_read_latency.py` | All read routers | Wave 2 — perf harness |
| NFR-2 (encryption + no PII in logs) | US-5 | `backend/tests/test_crypto.py`, `test_logging.py::test_no_email_in_logs` | `app/core/crypto.py`, `app/main.py` logging config | Implemented; partial test coverage |
| NFR-3 (graceful degradation) | US-6 | `backend/tests/test_tradelocker.py::test_account_state_graceful_degrade` | `app/api/tradelocker.py`, `app/api/dashboard.py` | Implemented; Tested |
| NFR-4 (WCAG 2.1 AA) | (all UI) | `frontend/tests/axe.spec.ts` | `frontend/app/*`, `WCAG_AUDIT.md` | Audit complete; remediations open |
| NFR-5 (compliance framing) | US-13 | manual review (LEGAL.md, marketing copy) | `LEGAL.md`, `frontend/app/page.tsx` | Verified |
| NFR-6 (auditability) | US-7, US-8 | `backend/tests/test_signal_router.py::test_execution_row_has_audit_fields` | `app/db/models.py:Execution`, `app/core/signal_router.py` | Implemented |
| NFR-7 (scalability 100 users / 1m) | US-9 | `backend/tests/perf/test_runner_soak.py` | `app/strategies/runner.py` | Wave 2 — soak harness |
| NFR-8 (browser support) | (all UI) | manual matrix per release | `frontend/*` | Wave 2 — BrowserStack |
| NFR-9 (deployable) | US-15 | CI workflow `lint`, `test`, `docker-build` | `.github/workflows/ci.yml`, `Dockerfile` | Verified |
| NFR-10 (data retention) | (admin) | runbook | `docs/RUNBOOK_RETENTION.md` (Wave 2) | Open |

---

## Open items rolling up to score

| Severity | Item | Linked risk |
|----------|------|-------------|
| Critical | Mount `auth.router` in `app/main.py` and add login → `/me` smoke test. | R-04 |
| High | Add WCAG remediations 1–5 (focus indicator, border contrast, skip link, BMC aria-label, error association). | NFR-4 |
| High | Add HMAC signature on TradingView webhook. | R-12 |
| Medium | Build perf harness for NFR-1. | NFR-1 |
| Medium | Build soak harness for NFR-7. | NFR-7 |
| Medium | Validate SQLite backup restore drill. | R-15 |

---

## How to update this file

1. New FR or NFR → add a row to `REQUIREMENTS.md` and a row here.
2. New user story → link from the new FR row's "User Story" column.
3. New test → fill in the "Test File" column with the path + nodeid.
4. Status moves from `Implemented` → `Tested` (CI green) → `Verified` (manual or staging-confirmed).

---

## Cross-References

- `REQUIREMENTS.md`
- `USER_STORIES.md`
- `RISK_MATRIX.md`
- `API.md`
- `WCAG_AUDIT.md`
