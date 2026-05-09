# WebSocket Protocol — Trade Copilot

Version 1 · 2026-05-08

All three implementing waves (3A backend, 3B TL relay, 3C frontend) MUST conform to this contract.

## Connection

- URL (dev): `ws://localhost:8000/ws`
- URL (prod): `wss://api.tradecopilot.example/ws`
- Subprotocol: not used
- Origin: `http://localhost:3001` (dev) / Vercel URL (prod) — backend validates against `ALLOWED_ORIGINS`

## Frames

All frames are JSON-encoded text frames with a `type` discriminator.

### Auth — required first frame after connect

```json
// client → server
{ "type": "auth", "token": "<jwt>" }

// server → client (success)
{ "type": "auth_ok", "email": "butler135@gmail.com", "user_id": 1 }

// server → client (failure, then closes with code 4401)
{ "type": "auth_failed", "reason": "invalid_token" }
```

The token is the same JWT issued by `POST /api/auth/login`. Client may pass it via:
- this `auth` frame (preferred), or
- `Authorization: Bearer <token>` header on the WS upgrade request, or
- `?token=...` query param (last resort, mostly for browser WS lack of headers).

### Subscribe / unsubscribe

```json
// → subscribe
{ "type": "subscribe", "channel": "account" }
{ "type": "subscribe", "channel": "positions" }
{ "type": "subscribe", "channel": "trades" }
{ "type": "subscribe", "channel": "signals" }
{ "type": "subscribe", "channel": "strategy:1m" }
{ "type": "subscribe", "channel": "strategy:5m" }

// ← ack
{ "type": "subscribed", "channel": "account" }

// → unsubscribe
{ "type": "unsubscribe", "channel": "strategy:1m" }
{ "type": "unsubscribed", "channel": "strategy:1m" }
```

Channels are scoped to the authenticated user. A client cannot subscribe to another user's data.

### Server-pushed events

```json
// account state changed
{
  "type": "event",
  "channel": "account",
  "ts": 1728432000123,
  "data": {
    "balance": 9999.78,
    "equity": 9999.78,
    "available_funds": 9999.78,
    "open_pnl": 0.0,
    "today_net": -0.22,
    "positions_count": 0
  }
}

// position opened/closed
{
  "type": "event",
  "channel": "positions",
  "ts": 1728432000123,
  "data": {
    "kind": "opened" | "closed" | "updated",
    "id": "7566047373987462950",
    "symbol": "BTCUSD",
    "side": "buy",
    "qty": 0.01,
    "avg_price": 80295.8,
    "unrealized_pl": -0.13
  }
}

// trade closed (post-mortem with R-multiple)
{
  "type": "event",
  "channel": "trades",
  "data": { /* TradeOutcome JSON */ }
}

// strategy signal fired
{
  "type": "event",
  "channel": "signals",
  "data": { "bot_id": 4, "symbol": "BTCUSD", "side": "buy", "confidence": 1.84 }
}

// strategy state snapshot
{
  "type": "event",
  "channel": "strategy:1m",
  "data": {
    "state": { /* StrategyState */ },
    "performance": { /* PerformanceSnapshot or null */ },
    "recent_trades": [ /* last 20 TradeOutcome */ ]
  }
}
```

### Commands (client → server)

Commands are bi-directional RPC. Each carries a client-generated `cmd_id` for matching responses.

```json
// → start a strategy
{ "type": "command", "cmd_id": "abc-1", "name": "strategy.start",
  "params": { "bot_id": 4, "timeframe": "1m", "symbols": ["BTCUSD","ETHUSD"] } }

// → stop
{ "type": "command", "cmd_id": "abc-2", "name": "strategy.stop",
  "params": { "bot_id": 4, "timeframe": "1m" } }

// → adjust risk slider
{ "type": "command", "cmd_id": "abc-3", "name": "subscription.update",
  "params": { "subscription_id": 1, "aggression_level": 7 } }

// ← ok
{ "type": "command_ok", "cmd_id": "abc-1", "result": { ... } }

// ← error
{ "type": "command_error", "cmd_id": "abc-1", "code": "validation", "message": "..." }
```

### Heartbeat

```json
// either side
{ "type": "ping", "ts": 1728432000123 }
{ "type": "pong", "ts": 1728432000123 }
```

Server sends `ping` every 30s. Client must reply within 10s or the connection is closed with code 4408 (request timeout).

### Errors

```json
{ "type": "error", "code": "rate_limited" | "unauthorized" | "bad_frame", "message": "..." }
```

## Close codes

| Code | Meaning |
|------|---------|
| 1000 | Normal closure |
| 4401 | Auth required / failed |
| 4403 | Forbidden (subscribed to another user's channel) |
| 4408 | Heartbeat timeout |
| 4429 | Rate limit on connection |

## Rate limits

- Max 1 connection per user (older one closed when new connects)
- Max 10 frames/sec per connection
- Subscriptions: max 16 channels per connection

## Backend implementation contract (Wave 3A)

- File: `backend/app/ws/server.py` — accepts `/ws` upgrades
- File: `backend/app/ws/connection_manager.py` — tracks `{user_id → set[WebSocket]}`
- File: `backend/app/ws/event_bus.py` — `await bus.publish(channel, user_id, payload)`; subscribers get pushed
- File: `backend/app/ws/handlers.py` — frame routing (`auth`/`subscribe`/`command`/`ping`)
- Mounted at `/ws` (no /api prefix — standard for WS)

## TradeLocker relay contract (Wave 3B)

- File: `backend/app/ws/tradelocker_relay.py`
- Per active user, runs an asyncio task that polls `/trade/accounts/{id}/state` + `/positions` every 2 seconds
- On state delta vs last seen, calls `event_bus.publish("account", user_id, new_state)` or `event_bus.publish("positions", user_id, position_event)`
- If TradeLocker exposes a real WS, switch this task to subscribe; document discovery in code comments

## Frontend implementation contract (Wave 3C)

- File: `frontend/lib/ws.ts` — `WebSocketClient` class with auto-reconnect (exponential backoff 1s→30s)
- File: `frontend/hooks/useWebSocket.ts` — React hook, returns `{ status, subscribe, unsubscribe, command, lastMessage }`
- Replace `setInterval` polling on `/strategy`, `/dashboard`, `/connect` pages
- Subscribe to relevant channels on mount, unsubscribe on unmount
- Send commands for start/stop/risk-slider instead of REST
