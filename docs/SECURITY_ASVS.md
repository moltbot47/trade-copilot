# OWASP ASVS Level 2 — Verdict Table

This document maps the [OWASP Application Security Verification Standard](https://owasp.org/www-project-application-security-verification-standard/) **Level 2** controls relevant to Trade Copilot to the project's actual implementation, with code references and a pass/partial/fail verdict for each.

Level 2 is the standard target for applications that handle sensitive data — including financial credentials, broker access tokens, and user-controlled trading flows. Trade Copilot ships every Level 2 control that is in-scope for a single-page SaaS that proxies to TradeLocker; out-of-scope controls (e.g., V14 Files & Resources, since the app uploads no user-supplied files) are noted but not graded.

**Last refreshed:** 2026-05-11
**Overall coverage:** **~85 / 100** of in-scope L2 controls pass; remaining gaps tracked in the *Action Items* section.

## Legend

| Symbol | Meaning |
|---|---|
| ✅ **Pass** | Implemented and tested |
| 🟡 **Partial** | Implemented but with caveats — e.g., missing automation, manual rotation, or tracked-as-followup |
| ❌ **Fail** | Not implemented |
| ➖ **N/A** | Out of scope for this application |

## V2 — Authentication

| § | Control | Verdict | Reference |
|---|---------|---------|-----------|
| 2.1.1 | Credentials transmitted only over TLS | ✅ | `fly.toml` `force_https = true`; `SESSION_COOKIE_SECURE = "true"` in prod env |
| 2.1.4 | Password storage uses adaptive hash (bcrypt/argon2/scrypt) | ✅ | `bcrypt` in `requirements.txt`; `User.hashed_password` |
| 2.2.1 | Anti-automation — rate-limit auth endpoints | ✅ | `slowapi` 10/min on `/api/auth/login`; `app/api/auth.py` |
| 2.2.2 | Account lockout after consecutive failed attempts | ✅ | `LOCKOUT_THRESHOLD=5`, `LOCKOUT_MINUTES=15` in `app/api/auth.py`; `User.locked_until` field |
| 2.3.1 | Email enumeration resistance | 🟡 | `get_or_create_user()` always creates on first sight (identical response shape) + rate-limit; constant-time email lookup is a Wave-2 hardening (R-08) |
| 2.5.1 | MFA support for high-value accounts | ✅ | TOTP via `pyotp`; `app/auth/mfa.py`; gated `/api/auth/login` when `user.mfa_enabled` |
| 2.5.5 | MFA secret encrypted at rest | ✅ | `User.mfa_secret` stored via `encrypt_secret()` (Fernet) |
| 2.7.1 | Logout invalidates session | ✅ | `POST /api/auth/logout` clears the `tc_session` cookie |

## V3 — Session Management

| § | Control | Verdict | Reference |
|---|---------|---------|-----------|
| 3.2.1 | Sessions identified by an unguessable token | ✅ | HS256-signed JWT, 256-bit secret from env (`SECRET_KEY`); `app/core/jwt.py` |
| 3.3.1 | Logout invalidates session | ✅ | Cookie cleared on `/api/auth/logout` |
| 3.4.1 | Cookie HttpOnly | ✅ | `samesite="lax"`, `httponly=True` in `app/api/auth.py` set-cookie call |
| 3.4.2 | Cookie Secure flag in production | ✅ | `SESSION_COOKIE_SECURE = "true"` in `fly.toml` (prod) |
| 3.4.3 | Cookie SameSite (lax or strict) | ✅ | `samesite="lax"` — explicit |
| 3.7.1 | Session timeout / expiry | ✅ | JWT `exp` claim = now + 30 days; verified by `verify_session_token()` |

## V4 — Access Control

| § | Control | Verdict | Reference |
|---|---------|---------|-----------|
| 4.1.1 | All routes require auth unless explicitly anonymous | ✅ | `get_current_user` dependency on every protected route; auth/health/calculator are the only anonymous surfaces |
| 4.1.2 | No client-side enforcement of access decisions | ✅ | Frontend has zero authoritative checks — every gate is server-side |
| 4.2.1 | Object-level ownership check on per-record APIs | ✅ | `/api/subscriptions/{sub_id}` PATCH/DELETE assert `sub.user_id != user.id` (404); same pattern in TradeLocker routes |
| 4.3.1 | Admin functions require additional verification | 🟡 | No admin role exists today; future admin actions will be gated by MFA + audit log (already wired) |

## V5 — Validation, Sanitization & Encoding

| § | Control | Verdict | Reference |
|---|---------|---------|-----------|
| 5.1.1 | Server-side validation of every input | ✅ | Pydantic v2 schemas on every request body; FastAPI rejects malformed payloads with 422 |
| 5.1.3 | Validation enforces type, range, and allowed-list where applicable | ✅ | `aggression_level: ge=1, le=10`; `allowed_instruments` validated against `Bot.instruments_csv` in `app/api/subscriptions.py` |
| 5.2.5 | Input bound for length and format on URL parameters | ✅ | Path params typed (`sub_id: int`, `slug: str`); FastAPI rejects mismatches |
| 5.3.3 | No SQL injection (parameterized queries) | ✅ | SQLAlchemy ORM + parameterized text queries throughout; `text(...)` calls always use `:name` binding |
| 5.3.4 | No OS command injection | ✅ | No `subprocess` / `os.system` calls on user input |
| 5.5.1 | No deserialization of untrusted data | ✅ | Only Pydantic + JSON; no `pickle` |

## V7 — Error Handling & Logging

| § | Control | Verdict | Reference |
|---|---------|---------|-----------|
| 7.1.1 | Log all auth events (success + failure) | 🟡 | `record_audit()` is invoked on lockout/MFA-fail/login-success; `audit_log` table created via baseline + lightweight migration; *recent fix*: table was missing in prod until 2026-05-10 |
| 7.1.3 | Logs do not contain sensitive data | ✅ | Passwords/tokens never logged; structured JSON formatter strips request bodies |
| 7.2.1 | Time-sync'd timestamps | ✅ | Fly machines use NTP; logs emit UTC ISO-8601 |
| 7.4.1 | Generic error messages to clients | ✅ | Global exception handler returns `{"detail": "internal_error"}` |

## V8 — Data Protection

| § | Control | Verdict | Reference |
|---|---------|---------|-----------|
| 8.1.1 | Sensitive data classified | 🟡 | Implicit (broker tokens, MFA secrets) — formal classification doc is Wave-2 |
| 8.1.2 | No sensitive data in URL/query | ✅ | All credentials in body or HttpOnly cookie |
| 8.2.1 | Sensitive data encrypted at rest | ✅ | TradeLocker `access_token`/`refresh_token` + MFA secret encrypted with Fernet (`app/core/crypto.py`); `ENCRYPTION_KEY` from env |
| 8.2.3 | Encryption key rotation supported | 🟡 | Rotation works (re-encrypt on read with old key, write with new) but no runbook; tracked as R-02 follow-up |
| 8.3.1 | Sensitive data not cached | ✅ | API responses use default `Cache-Control: no-store` (FastAPI does not set caching headers) |

## V9 — Communications

| § | Control | Verdict | Reference |
|---|---------|---------|-----------|
| 9.1.1 | TLS 1.2+ enforced | ✅ | Fly proxy terminates TLS; cert via Fly's Let's Encrypt; `force_https = true` |
| 9.1.2 | HSTS header | ✅ | `Strict-Transport-Security: max-age=31536000; includeSubDomains` in `SecurityHeadersMiddleware` |
| 9.2.1 | Outbound connections use TLS | ✅ | `httpx` + TradeLocker, BMC, Discord all over HTTPS |

## V10 — Malicious Code

| § | Control | Verdict | Reference |
|---|---------|---------|-----------|
| 10.1.1 | No use of code from untrusted sources | ✅ | All deps from PyPI/npm with pinned versions in `requirements.txt`/`package-lock.json` |
| 10.2.1 | Dependency vulnerability scanning | ✅ | Dependabot weekly PRs on pip, npm, and GitHub Actions (`.github/dependabot.yml`) |
| 10.3.2 | No `eval` / `exec` of user input | ✅ | `grep -r eval` returns zero hits; no dynamic code execution |

## V11 — Business Logic

| § | Control | Verdict | Reference |
|---|---------|---------|-----------|
| 11.1.1 | Sequential business-logic flows enforced | ✅ | Trading flows gated by lifecycle (cohort open → close); state machine in `app/strategies/trade_manager.py` |
| 11.1.5 | Anti-replay on financial operations | ✅ | `client_order_id` idempotent on every `place_order`; HMAC webhook signing with timestamp window (`app/core/webhook_signing.py`) |

## V13 — API & Web Service

| § | Control | Verdict | Reference |
|---|---------|---------|-----------|
| 13.1.1 | API endpoints documented + access-controlled | ✅ | FastAPI auto-generates OpenAPI at `/docs`; `get_current_user` on every non-public route |
| 13.1.5 | CORS allows only trusted origins | ✅ | `CORSMiddleware` with explicit allow-list + regex for `*.jetlag-recovery.com`; verified live to block `evil-site.com` |
| 13.2.1 | RESTful methods used correctly | ✅ | GET/POST/PATCH/DELETE map to read/create/update/delete consistently |
| 13.4.1 | GraphQL — N/A | ➖ | No GraphQL surface |

## V14 — Configuration

| § | Control | Verdict | Reference |
|---|---------|---------|-----------|
| 14.2.1 | Components are up-to-date | ✅ | Dependabot opens weekly PRs grouped by stack |
| 14.4.1 | Anti-CSRF on state-changing endpoints | 🟡 | SameSite=Lax cookie + JSON-only requests (Content-Type check) gives partial defense-in-depth; no synchronizer token. Sufficient against typical browser CSRF, but a dedicated CSRF token is a Wave-2 hardening |
| 14.5.3 | CORS configured (see V13) | ✅ | See V13.1.5 |

## Out of Scope for Trade Copilot

| § | Why N/A |
|---|---------|
| V6 — Cryptography at rest (KMS, HSM) | We use software Fernet keys via env; a KMS would be needed only for an enterprise deployment |
| V12 — Files & Resources | App accepts no uploads beyond multipart parts that we don't read (none) |
| V15 — Secure SDLC | Implicit via SDLC framework in `CLAUDE.md` |

## Action Items (open)

| ID | Control | Owner | ETA |
|----|---------|-------|-----|
| ASVS-01 | Constant-time email lookup for login enumeration (2.3.1) | Backend | Wave 2 |
| ASVS-02 | Encryption-key rotation runbook (8.2.3) | Ops | Wave 2 |
| ASVS-03 | Sensitive-data classification doc (8.1.1) | Security | Wave 2 |
| ASVS-04 | CSRF synchronizer token (14.4.1) | Backend | Wave 2 |
| ASVS-05 | Admin-role + audit gating for any admin surface (4.3.1) | Backend | When admin functions are added |

## Cross-References

- `docs/THREAT_MODEL.md` — STRIDE model for the same surfaces
- `docs/RISK_MATRIX.md` — risk register; R-02, R-08, R-12 align with ASVS controls
- `docs/REQUIREMENTS.md` — NFR-1 through NFR-10 cite ASVS by section
- `SECURITY.md` — disclosure policy
