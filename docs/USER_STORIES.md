# Trade Copilot — User Stories

This file lists every user story for v0.1, grouped by persona. Each story carries acceptance criteria, a priority (P0/P1/P2), a story-point estimate, and the FR ID(s) it satisfies. Stories are the contract between Phase 1 (Requirements) and Phase 4 (Testing) — every acceptance criterion must be exercised by an automated test, tracked in `REQUIREMENTS_TRACEABILITY.md`.

---

## Personas

| Persona | Description |
|---------|-------------|
| **Donor / Casual user** | Curious visitor. Mostly reads, occasionally tips a coffee. Cares about clarity, legality, and the calculator PDF. |
| **Active trader** | Has a Genesis FX account. Wants to plug it in, pick a strategy, and watch trades fire. Cares about latency, risk, and the dashboard's truthfulness. |
| **Strategy developer** | Writes Pine scripts, posts webhooks. Cares about the webhook contract, instrument mapping, and signal logs. |
| **Operator / admin** | Runs the service. Cares about health endpoints, structured logs, deployment, and audit trails. |

---

## Stories

### US-1 — Browse the bot catalog (Donor / Casual user)
> As a casual visitor, I want to see the list of available strategy bots so that I can decide whether the product is worth my attention.

- **Acceptance criteria**
  - `GET /api/bots` returns ≥ 1 bot when the database is seeded.
  - Each bot exposes `name`, `slug`, `description`, `backtest_win_rate`, `backtest_profit_factor`, `risk_level`, `instruments_csv`.
  - `is_active=false` bots are excluded.
  - The `/bots` page on the frontend renders one card per bot with no JS errors.
- **Priority**: P0 — **Points**: 2 — **FR**: FR-3

### US-2 — Email-only login (Donor / Casual user, Active trader)
> As any user, I want to log in with just my email so that I can start using the app without creating a password.

- **Acceptance criteria**
  - `POST /api/auth/login` with `{email}` returns 200, sets `tc_session` HttpOnly cookie.
  - Cookie has `Secure=true` in production, `SameSite=Lax`.
  - First-time emails auto-create a `User` row.
  - `GET /api/auth/me` with the cookie returns the user; without it returns 401.
  - Login is rate-limited to 10/min per IP.
- **Priority**: P0 — **Points**: 3 — **FR**: FR-1, FR-2

### US-3 — Subscribe to a bot (Active trader)
> As an active trader, I want to subscribe to a bot at a chosen aggression level so that the system trades it on my account.

- **Acceptance criteria**
  - `POST /api/subscriptions` with `{bot_id, aggression_level}` returns 201 + the new subscription.
  - Aggression is bounded 1–10; out-of-range returns 422.
  - Duplicate `(user_id, bot_id)` returns 409.
  - Subscription appears in `GET /api/subscriptions`.
- **Priority**: P0 — **Points**: 3 — **FR**: FR-4

### US-4 — Adjust or pause a subscription (Active trader)
> As an active trader, I want to change my aggression dial or pause a bot so that I can react to market conditions without losing my settings.

- **Acceptance criteria**
  - `PATCH /api/subscriptions/{id}` with `{aggression_level}` updates the row.
  - `PATCH` with `{is_paused: true}` halts further executions for that subscription.
  - Patching another user's subscription returns 404 (no info leak).
  - `DELETE /api/subscriptions/{id}` returns `{"status":"deleted"}` and removes the row.
- **Priority**: P0 — **Points**: 2 — **FR**: FR-5

### US-5 — Connect Genesis FX (Active trader)
> As an active trader, I want to connect my Genesis FX TradeLocker account so that signals can route to my live or demo account.

- **Acceptance criteria**
  - `POST /api/tradelocker/connect` with email/password/server/env returns `{"status":"connected"}` on success.
  - On failure, the API returns a user-friendly 400 with one of: invalid credentials, server not recognized, network/timeout, generic.
  - On success, `tradelocker_token`, `tradelocker_refresh_token`, and `tradelocker_email` are stored encrypted (Fernet).
  - `tradelocker_account_id` and `acc_num` are populated.
  - The connect form's server input pattern enforces `^[A-Za-z0-9_-]+$`.
- **Priority**: P0 — **Points**: 5 — **FR**: FR-6

### US-6 — See my account state (Active trader)
> As an active trader, I want to see my balance, equity, and open PnL on the dashboard so that I trust the system is talking to my real account.

- **Acceptance criteria**
  - `GET /api/tradelocker/account` returns `connected: true` and balance/equity/availableFunds/openPnL.
  - When TradeLocker is unreachable, the endpoint returns `connected: true` with the cached `account_id` but null financials (no 5xx).
  - Disconnected users see `connected: false` and zero financials.
- **Priority**: P0 — **Points**: 3 — **FR**: FR-7, NFR-3

### US-7 — Receive TradingView signals (Strategy developer)
> As a strategy developer, I want to POST a signal to the webhook so that all subscribers of my bot get the trade.

- **Acceptance criteria**
  - `POST /api/webhooks/tradingview` with a valid payload returns 200, persists a `Signal`, and triggers fan-out.
  - `bot_secret` matches a `Bot.slug`; unknown or inactive bots return 404.
  - Response includes `signal_id` and `subscribers_notified`.
  - Discord webhook receives a formatted message (best-effort; webhook returns 200 even if Discord errors).
  - Each fan-out attempt creates an `Execution` row.
- **Priority**: P0 — **Points**: 5 — **FR**: FR-8, FR-9, FR-17

### US-8 — Cap my daily loss (Active trader)
> As an active trader, I want the system to stop placing trades after I'm down 3% on the day so that one bad signal doesn't wipe my account.

- **Acceptance criteria**
  - The risk engine reads `User.max_daily_loss_pct` (default 3.0).
  - Once realized + unrealized PnL crosses the threshold, new orders are rejected with status `rejected` and a reason.
  - Threshold is configurable per-user (Wave 2: UI; MVP: DB-only).
  - The decision is logged for audit (NFR-6).
- **Priority**: P0 — **Points**: 3 — **FR**: FR-10

### US-9 — Run the LaT-PFN momentum strategy (Active trader)
> As an active trader, I want to start the LaT-PFN momentum runner so that the model can make autonomous trade decisions on my chosen symbols.

- **Acceptance criteria**
  - `POST /api/strategy/start` with `{bot_id, timeframe, symbols, user_emails}` starts a runner; response confirms `task_alive: true`.
  - `POST /api/strategy/stop` halts the runner cleanly.
  - `GET /api/strategy/status?bot_id=…&timeframe=…` returns runner state, latest snapshot, and recent trades.
  - Starting on a non-`latpfn_momentum` bot returns 400.
- **Priority**: P1 — **Points**: 8 — **FR**: FR-11

### US-10 — Watch the equity curve (Active trader)
> As an active trader, I want to see my cumulative R-multiple and USD PnL over time so that I can judge a bot's performance at a glance.

- **Acceptance criteria**
  - `GET /api/strategy/equity?bot_id=…` returns aligned arrays of timestamps, cumulative R, and cumulative USD.
  - The frontend `EquityCurve` chart renders without flicker on update.
  - Empty datasets render an empty state rather than a JS error.
- **Priority**: P1 — **Points**: 3 — **FR**: FR-12, FR-13

### US-11 — Auto-tune the threshold (Strategy developer)
> As a strategy developer, I want the runner to tighten/loosen its confidence threshold based on rolling performance so that it self-correct without my intervention.

- **Acceptance criteria**
  - Every 20 closed trades, a `PerformanceSnapshot` is written.
  - `feedback_action` ∈ `{tighten, loosen, pause, hold}` based on win rate, profit factor, and drawdown.
  - The next snapshot's `threshold_after` reflects the action taken.
  - Pauses are timeboxed via `StrategyState.paused_until`.
- **Priority**: P1 — **Points**: 5 — **FR**: FR-13, FR-14

### US-12 — Generate a compound-growth PDF (Donor / Casual user)
> As a curious visitor, I want a personalized compound-growth PDF so that I can imagine what the system might do for me — and bookmark the brand.

- **Acceptance criteria**
  - `POST /api/calculator/generate` with `{start_balance, daily_rate_pct, days, requester_label}` returns a PDF stream with `Content-Disposition: attachment`.
  - `POST /api/calculator/preview` returns JSON with `end_balance`, `multiplier`, and milestone snapshots.
  - Inputs are bounded: start 0–10M, rate 0–10%, days 7–365.
  - The endpoint is unauthenticated (lead-gen tool).
- **Priority**: P1 — **Points**: 3 — **FR**: FR-15

### US-13 — Donate via Buy Me a Coffee (Donor / Casual user)
> As a happy user, I want a one-click donate button so that I can support the project without filling forms.

- **Acceptance criteria**
  - The `BMCButton` component links to `https://www.buymeacoffee.com/dbutler` with `target="_blank" rel="noopener"`.
  - The button appears on the homepage hero, the homepage CTA card, and the persistent layout footer.
  - No payment data ever touches the Trade Copilot backend.
- **Priority**: P0 — **Points**: 1 — **FR**: FR-18, NFR-5

### US-14 — Health probe (Operator / admin)
> As an operator, I want a public liveness endpoint so that load balancers and uptime monitors can detect a dead process without needing credentials.

- **Acceptance criteria**
  - `GET /health` returns `{"status":"ok"}` with HTTP 200.
  - The endpoint requires no auth and reads no DB.
  - Response time p95 < 50 ms.
- **Priority**: P0 — **Points**: 1 — **FR**: FR-16

### US-15 — Production-safe boot (Operator / admin)
> As an operator, I want the service to refuse to start in production with placeholder secrets so that I never deploy a forge-able JWT or a default Fernet key.

- **Acceptance criteria**
  - With `ENVIRONMENT=production` and `SECRET_KEY` or `ENCRYPTION_KEY` starting with `change_me`, the app raises `RuntimeError` at lifespan startup.
  - In `development`, placeholders are tolerated (with a log warning is acceptable).
- **Priority**: P0 — **Points**: 2 — **FR**: NFR-2

---

## Story Point Summary

| Priority | Stories | Points |
|----------|---------|--------|
| P0 | US-1, US-2, US-3, US-4, US-5, US-6, US-7, US-8, US-13, US-14, US-15 | 30 |
| P1 | US-9, US-10, US-11, US-12 | 19 |
| P2 | (none yet — backlog stories live in `PHASE_ROADMAP.md`) | 0 |
| **Total v0.1** | **15** | **49** |

---

## Cross-References

- `REQUIREMENTS.md` — the parent FR/NFR list.
- `REQUIREMENTS_TRACEABILITY.md` — story → test → source mapping.
- `API.md` — endpoint contracts the stories rely on.
