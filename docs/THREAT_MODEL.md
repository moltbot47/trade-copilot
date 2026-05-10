# Trade Copilot — STRIDE Threat Model

**As-of:** 2026-05-10
**Scope:** Backend (FastAPI on Fly.io), Frontend (Next.js on Vercel), database (SQLite/Postgres), and the TradeLocker broker integration.
**Methodology:** STRIDE (Spoofing, Tampering, Repudiation, Information Disclosure, Denial-of-Service, Elevation of Privilege). Companion document to [`RISK_MATRIX.md`](RISK_MATRIX.md), which scores broader business / operational risks.

Trade Copilot trades **real money** on a user's behalf. A successful attack maps directly to financial loss for users — so this model errs on the side of enumerating residual risk rather than declaring threats closed.

---

## 1. Assets

Ranked by blast radius if compromised.

| # | Asset | Where it lives | Impact if compromised |
|---|-------|----------------|-----------------------|
| A1 | `ENCRYPTION_KEY` (Fernet root key) | Fly secrets / `.env` (`app/config.py:24`) | Decrypts every TL token + MFA secret in the DB. Game-over. |
| A2 | TradeLocker access/refresh tokens | `users.tradelocker_token`, `tradelocker_refresh_token` (Fernet-encrypted, `app/db/models.py:62`) | Attacker can place, modify, and close trades on the user's prop-firm account. |
| A3 | TOTP / MFA shared secrets | `users.mfa_secret` (Fernet-encrypted, `app/db/models.py:92`) | Bypasses second factor; attacker can fully take over the account given email + DB read. |
| A4 | `SECRET_KEY` (JWT signing) | Fly secrets / `.env` (`app/config.py:19`) | Forge session cookies for any email → full account takeover without password or MFA. |
| A5 | Per-bot `webhook_secret` (HMAC) | `bots.webhook_secret` (`app/db/models.py:125`) | Inject fake TradingView signals → arbitrary entries fan out to subscribers. |
| A6 | Trade history / P&L / forecasts | `trade_outcomes`, `signals`, `cohorts`, `strategy_tick_log` | Reputational + privacy harm; competitive disclosure of strategy. |
| A7 | Discord webhook URL | `DISCORD_WEBHOOK_URL` env | Spam the community channel; impersonate the bot. |
| A8 | User email addresses | `users.email` (`app/db/models.py:54`) | PII; enables targeted phishing. |

---

## 2. Trust Boundaries & Data Flows

```
[User browser] --HTTPS--> [Vercel (Next.js)] --HTTPS--> [Fly.io backend (FastAPI)] --HTTPS--> [TradeLocker API]
                                                                  |
                                                                  +--> [SQLite/Postgres DB]
                                                                  +--> [Discord webhook]
                                                                  +--> [Prometheus /metrics]

[TradingView] --HTTPS + HMAC--> [Fly.io backend]
```

Trust boundaries (`→` = traverses boundary):

* **TB1** Browser → Vercel: untrusted → frontend (cookies travel `SameSite=Lax`, `HttpOnly`).
* **TB2** Vercel → Fly: cross-origin; CORS allow-list in `app/main.py:441`.
* **TB3** Fly → TradeLocker: outbound; depends on TLS + token correctness.
* **TB4** TradingView → Fly: third-party producer; verified via HMAC headers (`app/core/webhook_signing.py`).
* **TB5** Operator / fly secrets → Fly process: only operator with deploy access can read `ENCRYPTION_KEY` / `SECRET_KEY`.

### Key data flows

1. **Login** — `POST /api/auth/login` (`app/api/auth.py:71`): email (+ optional `mfa_code`) → JWT `tc_session` cookie. MFA enforced when `users.mfa_enabled` is set.
2. **MFA setup/verify/disable** — `app/api/mfa.py`: server-generated TOTP secret, encrypted at rest, requires current code to disable.
3. **Connect TradeLocker** — `POST /api/tradelocker/connect` (`app/api/tradelocker.py:35`): user submits broker email + password + server → backend authenticates and stores encrypted tokens.
4. **Inbound signal** — `POST /api/webhooks/tradingview` (`app/api/webhooks.py:59`): HMAC-signed body → identify bot → fan out to subscribed users → place orders.
5. **Order placement** — Strategy runners decrypt the user's TL token and call `TradeLockerClient` to place real trades.

---

## 3. Threats (STRIDE)

Threats are tagged with the most relevant mitigation found in code. Where no mitigation exists, the threat appears in *Residual risks* (§4).

### S — Spoofing

| # | Threat | Mitigation (code reference) |
|---|--------|-----------------------------|
| S1 | Attacker forges `tc_session` cookie to log in as another user. | HS256 JWT signed with `SECRET_KEY`; production boot refuses placeholder (`app/config.py:44`, `app/core/jwt.py`). |
| S2 | Attacker logs in with only a victim's email (MVP "no password" flow). | TOTP MFA gate at `app/api/auth.py:92` — once enabled, email alone is insufficient. **Users who haven't enabled MFA remain vulnerable** → see R1. |
| S3 | Attacker spoofs TradingView and injects fake signals. | Per-bot HMAC-SHA256 over `f"{ts}.{body}"`, ±300s window, constant-time compare (`app/core/webhook_signing.py`). Generic 401 prevents slug enumeration (`app/api/webhooks.py:55`). |
| S4 | Attacker abuses the legacy `bot_secret`-in-body path (slug == public catalog data). | Legacy path still accepted; emits structured deprecation warning (`app/api/webhooks.py:124`). **Scheduled for removal** → R5. |
| S5 | Attacker re-uses captured signed webhook request. | 300-second freshness window + clock-skew bound (`app/core/webhook_signing.py:72`). No nonce store, so replay within 300s is still possible → R6. |
| S6 | Attacker abuses `Authorization: Bearer` header path to inject a token. | Token decode goes through same `verify_session_token` (`app/api/users.py:32`); no header-trust shortcut. |

### T — Tampering

| # | Threat | Mitigation |
|---|--------|------------|
| T1 | Attacker mutates JWT claims (e.g. swap `sub` to another email). | HMAC signature invalidates any change (`app/core/jwt.py:38`). |
| T2 | MITM modifies cookie / API response in transit. | `Secure` flag in production (`app/config.py:35`), `Strict-Transport-Security` header in `app/main.py:457`. |
| T3 | Attacker rotates the victim's MFA secret out from under them after hijacking a session. | `POST /auth/mfa/setup` returns 409 if MFA already enabled — must disable with current code first (`app/api/mfa.py:68`). |
| T4 | Attacker tampers with HMAC-signed body bytes between TradingView and Fly. | Signature is computed over the **raw** body bytes (`app/api/webhooks.py:64`, signed with `f"{ts}.{body}"`) — any flip fails verify. |
| T5 | Database row tampering (e.g. flip `is_active=False`, raise `max_lot_override`). | Only the backend writes to the DB; no admin endpoint exposed publicly. Operator with Fly access can mutate directly → R7. |
| T6 | Tampering with cohort / position rows mid-trade. | Single-writer model (one FastAPI process). Cohort + leg writes are committed atomically within the runner. |

### R — Repudiation

| # | Threat | Mitigation |
|---|--------|------------|
| R1 | User claims they did not place a trade ("the bot did it without me"). | Every signal/execution row carries `bot_id`, `user_id`, `received_at`, `created_at` (`app/db/models.py:149–181`). Per-tick decision log persists rationale in `strategy_tick_log`. |
| R2 | User claims they never enabled MFA / never connected TradeLocker. | Structured info-level logs at `app/api/mfa.py:107`, `app/api/auth.py:110`, `app/api/tradelocker.py:63`. **Logs are not tamper-evident** → R3. |
| R3 | Operator action (e.g. raising a user's `max_concurrent_positions`) is unattributed. | Currently no audit log for sensitive mutations — already tracked as P4 follow-up (task #101). |

### I — Information Disclosure

| # | Threat | Mitigation |
|---|--------|------------|
| I1 | DB dump exposes TL credentials in plaintext. | Fernet encryption of `tradelocker_token` / `refresh_token` / `mfa_secret` / `tradelocker_email` (`app/core/crypto.py`). Attacker needs DB **and** `ENCRYPTION_KEY`. |
| I2 | Login probe leaks "user exists" via timing or status. | `get_or_create_user` always creates on miss (`app/api/users.py:79`); MFA path uses indistinguishable `mfa_required` / `mfa_invalid_code` responses (`app/api/auth.py:96`). |
| I3 | Webhook errors leak which check failed (slug vs sig vs ts). | Single `_GENERIC_AUTH_ERROR = "invalid webhook"` (`app/api/webhooks.py:52`). |
| I4 | Stack traces expose secrets / connection strings to the user. | Global exception handler returns `{"detail":"internal_error"}` only (`app/main.py:470`). |
| I5 | Session cookie stolen via XSS. | `HttpOnly` + `SameSite=Lax` + `Secure` (prod) (`app/api/auth.py:48`); CSP at `app/middleware/security_headers.py:20` blocks inline scripts from untrusted origins. CSP currently allows `'unsafe-inline'` → R8. |
| I6 | TL token logged in plaintext. | `connect()` logs only the user email and an error string on failure (`app/api/tradelocker.py:63`). Decrypted token never crosses a log boundary. |
| I7 | `/metrics` endpoint exposes internal labels. | Mounted at root (`app/main.py:478`). Prometheus exposes path + method counts but not bodies. Endpoint is **not auth-gated** → R9. |
| I8 | Encryption key derivation reduces effective entropy. | `_fernet()` SHA-256s the env var into a 32-byte key (`app/core/crypto.py:14`). Acceptable for high-entropy inputs; collapses if `ENCRYPTION_KEY` is weak → R10. |

### D — Denial of Service

| # | Threat | Mitigation |
|---|--------|------------|
| D1 | Login flood / credential stuffing. | 10/min/IP on `/auth/login` (`app/api/auth.py:72`); 5/min on `/tradelocker/connect` (`app/api/tradelocker.py:36`); global 60/min default (`app/core/rate_limit.py:44`). |
| D2 | Webhook flood drains DB connections. | 60/min/IP global limiter applies. No per-bot quota → R11. |
| D3 | Strategy runner OOM from unbounded growth. | `strategy_tick_log` auto-archived to 10k rows per (bot, timeframe) on every boot (`app/main.py:138`); `BarFetcher._cache` bounded (task #85). |
| D4 | SQLite write contention under concurrent users. | Documented in R-07 (RISK_MATRIX.md) — 100-user soak ceiling; migrate to Postgres above that. |
| D5 | Adversary triggers expensive PDF generation. | 30/min/IP on `/calculator/generate` (`app/core/rate_limit.py:6`). |
| D6 | Rate limiter is in-memory only. | Single-instance Fly app; horizontal scale would require Redis backend → R12. |

### E — Elevation of Privilege

| # | Threat | Mitigation |
|---|--------|------------|
| E1 | One user accesses another user's data. | All authenticated routes resolve `User` via `get_current_user` (`app/api/users.py:44`); ORM queries scope by `user_id`. |
| E2 | Hijacked session changes another user's MFA. | MFA endpoints take the session-user as the subject — never an email parameter (`app/api/mfa.py:58`). |
| E3 | Webhook signer with one bot's secret triggers another bot. | `X-Bot-Slug` header pinned to the secret used to verify (`app/api/webhooks.py:94`); cross-bot replay fails the HMAC. |
| E4 | Operator with Fly SSH access promotes themselves silently. | Single deploy operator (founder); fly secrets are the trust anchor. No multi-operator audit → R13. |
| E5 | Subscription bypass — user without an active sub gets trades fanned to them. | `Subscription.is_paused`, `User.is_active`, `User.bot_paused` all consulted by the fan-out path. |

---

## 4. Residual Risks (not currently mitigated)

These are real gaps. Listed here so we cannot pretend otherwise.

| Ref | Residual risk | Severity |
|-----|---------------|---------|
| R1 | **Passwordless login** — any email that hasn't enabled MFA is a single-factor account, vulnerable to email account compromise. The MVP "frictionless email" UX is the root cause. | High |
| R2 | **No account lockout after N failed login attempts** — slowapi rate-limits IPs but does not lock the account. Tracked as task #102. | Medium |
| R3 | **Logs are not tamper-evident** — operator with shell access can edit/delete log files. No remote append-only sink. | Medium |
| R4 | **No backup-code / recovery path for MFA** — explicitly out of scope in `app/auth/mfa.py:14`. Lost-device flow is manual operator intervention. | Medium |
| R5 | **Legacy `bot_secret`-in-body webhook auth still accepted** (`app/api/webhooks.py:104`). Any leaked slug = unauthenticated fan-out trigger. | High |
| R6 | **No replay-nonce store for signed webhooks** — within the 300s freshness window, an intercepted signed request can be replayed. | Low–Medium |
| R7 | **No audit trail for operator DB writes** (e.g. the one-time `max_concurrent_positions` bump in `app/main.py:181`). Task #101. | Medium |
| R8 | **CSP allows `'unsafe-inline'`** for scripts and styles (`app/middleware/security_headers.py:20`) — required by the BMC widget. Reduces XSS containment. | Medium |
| R9 | **`/metrics` is unauthenticated** — exposes request volumes and paths to anyone who finds it. | Low |
| R10 | **`ENCRYPTION_KEY` strength is operator-dependent** — `crypto.py` SHA-256s whatever is provided; a weak env var collapses Fernet's security. No min-length check. | Medium |
| R11 | **No per-bot webhook quota** — a single noisy bot can consume the global 60/min IP budget for everyone behind a shared NAT. | Low |
| R12 | **Rate-limiter is in-memory** — horizontal scaling would silently de-fang the limiter. | Low (single instance today) |
| R13 | **Single operator with full Fly secrets access** — no separation-of-duty for production secrets. | Medium |
| R14 | **No key rotation runbook** — rotating `ENCRYPTION_KEY` would invalidate every stored TL/MFA secret. No re-encrypt migration exists. | Medium |
| R15 | **CORS allow-list is broad in dev** (`localhost:3000,3001`); production allow-list is one env var away from misconfiguration. | Low |
| R16 | **No dependency-pinning provenance / SBOM**; supply-chain attack on `pyjwt`, `cryptography`, or `pyotp` would be undetected. | Medium |

---

## 5. Action Items (prioritized)

1. **P0 — Remove legacy `bot_secret`-in-body webhook path.** Set a deprecation deadline; emit user-facing warnings via the bot owner dashboard; delete the branch in `app/api/webhooks.py:104`.
2. **P1 — Add account lockout after N failed login / MFA attempts** (task #102). Per-user counter on `users` table, exponential backoff, reset on success.
3. **P1 — Audit log for security-sensitive actions** (task #101): MFA enable/disable, TL connect, panic toggle, `max_lot_override` change, login from new IP. Append-only table.
4. **P1 — Add `ENCRYPTION_KEY` strength assertion** in `Settings.assert_production_safe` — reject keys < 32 chars or low entropy.
5. **P2 — Auth-gate `/metrics`** behind an operator token or IP allow-list.
6. **P2 — Implement MFA recovery codes** — 10 single-use codes generated at MFA enable, hashed at rest.
7. **P2 — Webhook replay-nonce store** — Redis SETEX of `sig` for 300s.
8. **P2 — Key rotation runbook** — script that decrypts with old key, re-encrypts with new, atomically swaps.
9. **P3 — Tighten CSP** — replace `'unsafe-inline'` with per-request nonces once the BMC widget is iframed or removed.
10. **P3 — SBOM + supply-chain hygiene** — `pip-audit` in CI, pin hashes in `requirements.txt`.
11. **P3 — Secondary operator with read-only Fly access** for separation of duty and bus-factor.

See [`SECURITY.md`](../SECURITY.md) for the disclosure process.
