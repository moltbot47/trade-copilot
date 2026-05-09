# Trade Copilot — API Reference

This document is the canonical endpoint reference for the Trade Copilot backend. It is hand-curated from `app/api/*.py`, `app/strategies/api.py`, `app/main.py`, and `app/schemas.py`. Every route is mounted under `/api` except `/health`, which is top-level. Authenticated routes require a valid `tc_session` cookie or `Authorization: Bearer <jwt>` header — see ADR-0004.

Base URL in development: `http://localhost:8000`.

---

## Conventions

- Content-Type: `application/json` for both request and response bodies, except `/api/calculator/generate` (returns `application/pdf`).
- Authentication errors → `401 Unauthorized` with `WWW-Authenticate: Bearer`.
- Validation errors → `422 Unprocessable Entity` (Pydantic).
- Resource not found / not yours → `404 Not Found` (we deliberately conflate "not yours" with "not found" to prevent enumeration).
- Conflict (already-exists) → `409 Conflict`.
- Server error → `500 Internal Server Error` with `{"detail":"internal_error"}` (the global handler swallows traces).

---

## Health

### `GET /health` — Liveness probe

| Field | Value |
|-------|-------|
| Auth | none |
| Status codes | 200 |

**Response 200**
```json
{ "status": "ok" }
```

```bash
curl https://api.example.com/health
```

---

## Auth (`/api/auth`)

### `POST /api/auth/login` — Issue session cookie

| Field | Value |
|-------|-------|
| Auth | none |
| Rate limit | 10 / minute / IP |
| Status codes | 200, 422, 429 |

**Request**
```json
{ "email": "user@example.com" }
```

**Response 200** — also sets `Set-Cookie: tc_session=<jwt>; HttpOnly; SameSite=Lax`
```json
{ "email": "user@example.com", "exp": 1746800000 }
```

```bash
curl -X POST -H "Content-Type: application/json" \
     -d '{"email":"user@example.com"}' \
     https://api.example.com/api/auth/login -i
```

### `POST /api/auth/logout` — Clear session cookie

| Field | Value |
|-------|-------|
| Auth | none (idempotent) |
| Status codes | 200 |

**Response 200**
```json
{ "status": "logged_out" }
```

### `GET /api/auth/me` — Current user

| Field | Value |
|-------|-------|
| Auth | required |
| Status codes | 200, 401 |

**Response 200**
```json
{
  "email": "user@example.com",
  "tradelocker_account_id": "1234567",
  "tradelocker_env": "demo"
}
```

---

## Users (`/api/users`)

### `POST /api/users` — Create user with password

| Field | Value |
|-------|-------|
| Auth | none |
| Status codes | 201, 409, 422 |

**Request**
```json
{ "email": "user@example.com", "password": "min6chars" }
```

**Response 201** — `UserOut`
```json
{
  "id": 1,
  "email": "user@example.com",
  "created_at": "2026-05-08T12:00:00",
  "is_active": true,
  "max_daily_loss_pct": 3.0,
  "tradelocker_account_id": null,
  "tradelocker_env": "demo"
}
```

### `GET /api/users/me` — Same as `/auth/me` but returns full `UserOut`

| Field | Value |
|-------|-------|
| Auth | required |
| Status codes | 200, 401 |

---

## Bots (`/api/bots`)

### `GET /api/bots` — List active bots

| Field | Value |
|-------|-------|
| Auth | none |
| Status codes | 200 |

**Response 200**
```json
[
  {
    "id": 1,
    "name": "ORB Breakout",
    "slug": "orb-breakout",
    "description": "Opening Range Breakout - trades the first 30-min range break.",
    "strategy_type": "orb",
    "backtest_win_rate": 62.0,
    "backtest_profit_factor": 1.4,
    "risk_level": 3,
    "instruments_csv": "EURUSD,GBPUSD,XAUUSD",
    "is_active": true,
    "created_at": "2026-05-08T12:00:00"
  }
]
```

### `GET /api/bots/{bot_id}` — Bot detail

| Field | Value |
|-------|-------|
| Auth | none |
| Status codes | 200, 404 |

---

## Subscriptions (`/api/subscriptions`)

All endpoints require auth.

### `GET /api/subscriptions` — List my subscriptions

**Response 200** — array of `SubscriptionOut`
```json
[
  {
    "id": 1,
    "user_id": 1,
    "bot_id": 1,
    "aggression_level": 5,
    "is_paused": false,
    "created_at": "2026-05-08T12:00:00",
    "updated_at": "2026-05-08T12:00:00"
  }
]
```

### `POST /api/subscriptions` — Subscribe to a bot

**Request**
```json
{ "bot_id": 1, "aggression_level": 5 }
```

| Field | Value |
|-------|-------|
| Status codes | 201, 401, 404 (bot), 409 (duplicate), 422 |

### `PATCH /api/subscriptions/{sub_id}` — Update aggression / pause

**Request** (any field optional)
```json
{ "aggression_level": 7, "is_paused": false }
```

| Field | Value |
|-------|-------|
| Status codes | 200, 401, 404, 422 |

### `DELETE /api/subscriptions/{sub_id}` — Unsubscribe

**Response 200**
```json
{ "status": "deleted" }
```

---

## TradeLocker (`/api/tradelocker`)

### `POST /api/tradelocker/connect` — Connect Genesis FX account

| Field | Value |
|-------|-------|
| Auth | required |
| Status codes | 200, 400 (bad creds / unknown server / network), 401 |

**Request**
```json
{
  "email": "trader@example.com",
  "password": "broker-password",
  "server": "GENFX",
  "env": "demo"
}
```

**Response 200**
```json
{ "status": "connected", "detail": "1234567" }
```

The 400 error messages are user-facing strings (see `tradelocker.py:connect`).

### `GET /api/tradelocker/account` — Account state

| Field | Value |
|-------|-------|
| Auth | required |
| Status codes | 200, 401 |

**Response 200** — `TradeLockerAccountOut`
```json
{
  "account_id": "1234567",
  "acc_num": "1",
  "server": "GENFX",
  "env": "demo",
  "balance": 50000.0,
  "equity": 50125.0,
  "available_funds": 49500.0,
  "open_pnl": 125.0,
  "today_net": 0.0,
  "positions_count": 1,
  "currency": "USD",
  "connected": true
}
```

When TradeLocker is unreachable, all numeric fields may be null but `connected` remains `true` (graceful degradation).

---

## Dashboard (`/api/dashboard`)

### `GET /api/dashboard/pnl` — Execution counts + realized PnL

| Field | Value |
|-------|-------|
| Auth | required |
| Status codes | 200, 401 |

**Response 200**
```json
{
  "total_executions": 42,
  "filled": 38,
  "rejected": 2,
  "errors": 1,
  "pending": 1,
  "realized_pnl": 0.0
}
```

### `GET /api/dashboard/positions` — Open positions from TradeLocker

| Field | Value |
|-------|-------|
| Auth | required |
| Status codes | 200, 401 |

**Response 200** — array of `PositionOut`
```json
[
  {
    "instrument": "EURUSD",
    "side": "buy",
    "quantity": 0.05,
    "avg_price": 1.0832,
    "unrealized_pnl": 12.5,
    "raw": { "...": "passthrough TradeLocker fields" }
  }
]
```

---

## Strategy (`/api/strategy`)

### `POST /api/strategy/start` — Start a runner

| Field | Value |
|-------|-------|
| Auth | required |
| Status codes | 200, 400 (wrong bot type / empty list), 401, 404 |

**Request**
```json
{
  "bot_id": 4,
  "timeframe": "1m",
  "symbols": ["EURUSD", "XAUUSD"],
  "user_emails": ["user@example.com"],
  "latpfn_endpoint": "https://latpfn.example.com/forecast"
}
```

**Response 200**
```json
{
  "status": "started",
  "bot_id": 4,
  "timeframe": "1m",
  "symbols": ["EURUSD", "XAUUSD"],
  "users": ["user@example.com"],
  "task_alive": true
}
```

### `POST /api/strategy/stop` — Stop a runner

**Request**
```json
{ "bot_id": 4, "timeframe": "1m" }
```

**Response 200** — `{"status":"stopped"}` or `{"status":"not_running"}`.

### `GET /api/strategy/status?bot_id=…&timeframe=…` — Runner status + perf

**Response 200**
```json
{
  "state": {
    "bot_id": 4,
    "timeframe": "1m",
    "is_running": true,
    "confidence_threshold": 1.5,
    "max_concurrent": 3,
    "paused_until": null,
    "last_tick_at": "2026-05-08T12:00:00",
    "last_signal_at": null,
    "last_error": null,
    "started_at": "2026-05-08T11:00:00"
  },
  "runner_alive": true,
  "performance": { "win_rate": 0.65, "profit_factor": 1.6, "...": "..." },
  "latest_snapshot": { "...": "alias of performance" },
  "recent_trades": [ { "id": 1, "instrument": "EURUSD", "...": "..." } ],
  "recent_outcomes": [ "...alias" ],
  "recent_snapshots": [ { "...": "..." } ]
}
```

When no `StrategyState` exists for that key, the endpoint returns a structured "not initialized" payload (200) so the dashboard can render an empty state without erroring.

### `GET /api/strategy/equity?bot_id=…` — Cumulative R-curve

**Response 200**
```json
{
  "bot_id": 4,
  "timestamps": ["2026-05-08T12:00:00", "2026-05-08T12:30:00"],
  "cumulative_r": [1.0, 2.5],
  "cumulative_pnl_usd": [50.0, 125.0],
  "total_trades": 2
}
```

---

## Calculator (`/api/calculator`)

Both endpoints are **unauthenticated** — they're a lead-gen tool.

### `POST /api/calculator/generate` — Compound-growth PDF

**Request**
```json
{
  "start_balance": 10000,
  "daily_rate_pct": 2.0,
  "days": 100,
  "requester_label": "Optional name in PDF"
}
```

| Bounds | start_balance ∈ (0, 10_000_000], daily_rate_pct ∈ (0, 10], days ∈ [7, 365], label ≤ 120 chars. |
|--------|---|

**Response 200** — `application/pdf`, with `Content-Disposition: attachment; filename="..."`.

### `POST /api/calculator/preview` — JSON preview (no PDF)

**Response 200**
```json
{
  "start_balance": 10000.0,
  "daily_rate_pct": 2.0,
  "days": 100,
  "end_balance": 72446.46,
  "multiplier": 7.24,
  "milestones": { "10": 12189.94, "25": 16406.06, "50": 26915.88, "75": 44158.31, "100": 72446.46 }
}
```

---

## Webhooks (`/api/webhooks`)

### `POST /api/webhooks/tradingview` — Receive a strategy signal

| Field | Value |
|-------|-------|
| Auth | none (signed-by-bot-secret model: payload carries `bot_secret = bot.slug`) |
| Status codes | 200, 404 (unknown bot), 422 |

**Request**
```json
{
  "bot_secret": "orb-breakout",
  "instrument": "EURUSD",
  "side": "buy",
  "entry_price": 1.0832,
  "stop_loss": 1.0810,
  "take_profit": 1.0900,
  "base_lot_size": 0.01
}
```

Extra TradingView fields are accepted via Pydantic `extra="allow"` and persisted in `Signal.raw_payload`.

**Response 200**
```json
{ "status": "received", "signal_id": 42, "subscribers_notified": 7 }
```

```bash
curl -X POST -H "Content-Type: application/json" \
  -d '{"bot_secret":"orb-breakout","instrument":"EURUSD","side":"buy","base_lot_size":0.01}' \
  https://api.example.com/api/webhooks/tradingview
```

---

## Error envelopes

Every non-2xx returns:
```json
{ "detail": "human-readable reason" }
```
For 422 Pydantic returns its standard validation-error array under `detail`.

---

## Cross-References

- `REQUIREMENTS.md` for what each endpoint must do.
- `USER_STORIES.md` for acceptance criteria.
- `ARCHITECTURE.md` for which component owns each route.
- `adr/0001-fastapi-backend.md` for why FastAPI / Pydantic.
- `adr/0004-jwt-cookie-auth.md` for the auth model.
