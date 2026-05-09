# ADR-0002 — TradeLocker REST + WS instead of an MT5 bridge

- **Status**: Accepted
- **Date**: 2026-05-08

## Context

Genesis FX (and most modern prop / retail FX brokers) expose two execution surfaces: TradeLocker (a web-native broker UI with a documented REST + WebSocket API at `live.tradelocker.com/backend-api`) and MetaTrader 5 (MT5), reachable from Python only via the closed-source `MetaTrader5` package on Windows or via a self-hosted bridge (e.g., MT5 ↔ ZeroMQ, `mt5linux`).

Trade Copilot must place orders on the user's behalf, read account state, and detect fills. We picked the surface.

## Decision

We adopt **TradeLocker REST + WebSocket** as the sole execution path.

- Auth: `POST /auth/jwt/token` with `{email, password, server}` returns an access token + refresh token. We persist them encrypted with Fernet (ADR-0004 covers the user-session cookie; this is a different secret).
- Account state: `GET /trade/accounts/{accountId}/state` (HTTP polling on the dashboard) and a WS feed for live PnL updates.
- Orders: `POST /trade/accounts/{accountId}/orders` with `{instrument, side, qty, …}`.

The full client lives in `app/core/tradelocker_client.py`.

## Consequences

**Positive**
- One stack (Python + httpx) — no Windows VM, no DLL bridge, no per-user terminal.
- Cross-platform — runs on the maintainer's Intel Mac and on a Linux Docker host without modification.
- TradeLocker's auth model is JWT-based and refresh-friendly, which fits naturally with our Fernet-encrypted token storage.
- Webhook + WS combo gives us sub-second order acknowledgement and PnL updates.

**Negative**
- TradeLocker's instrument naming differs from MT5; symbol mapping has to be maintained (e.g., `GENFX` server uses `EURUSD` directly but `XAUUSD` may be `XAU/USD` depending on the prop firm's setup).
- The REST API is undocumented for a few corners (e.g., `availableFunds` semantics around partial fills); we cope with defensive `getattr`-style parsing in the client.
- TradeLocker outages take the whole product offline. Mitigated by NFR-3 graceful degradation — the UI shows last-known balance instead of a 5xx.

## Alternatives considered

| Option | Why rejected |
|--------|--------------|
| MetaTrader 5 native (`MetaTrader5` pip pkg) | Windows-only, closed-source, single-process. |
| `mt5linux` bridge | Self-hosted Wine + remote-call layer; fragile, hard to deploy. |
| FIX directly | Genesis FX does not expose FIX to retail; institutional-only. |
| Manual / Discord-only signals | Defeats the auto-execution thesis; users still have to click. |

## Implementation notes

- Server name `GENFX` is hardcoded in the connect form's helper text and in error messages so users self-correct (`app/api/tradelocker.py`).
- Tokens live in `User.tradelocker_token` and `User.tradelocker_refresh_token` (both `Text`, both Fernet-encrypted).
- A future ADR will cover token refresh strategy when access tokens expire mid-runner.
