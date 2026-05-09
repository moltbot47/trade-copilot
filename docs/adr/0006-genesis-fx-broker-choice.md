# ADR-0006 — Genesis FX as the demo / launch broker

- **Status**: Accepted
- **Date**: 2026-05-08

## Context

Trade Copilot needs a broker that (a) supports retail and prop traders, (b) exposes a programmatic execution surface, (c) accepts US-based users, (d) doesn't require Trade Copilot itself to register as an introducing broker, and (e) is a known quantity for the maintainer's existing community.

The candidates were Genesis FX (TradeLocker), Topstep / Topstep X, Apex Trader Funding (Tradovate), and Oanda (own REST API). For the MVP we wanted exactly one broker with a known good integration path, so the catalog ships with one supported broker.

## Decision

**Genesis FX is the launch broker**, accessed via TradeLocker.

- The connect form's server name field defaults to `GENFX` and validates against `^[A-Za-z0-9_-]+$`.
- TradeLocker error messages are translated to Genesis-specific helper text (e.g., "Server name not recognized. Genesis FX uses 'GENFX'.") in `app/api/tradelocker.py`.
- Both demo and live environments are supported via `env: "demo" | "live"` on the connect payload.
- Marketing copy and the homepage hero ("Educational auto-trader for Genesis FX") name the broker explicitly.

## Consequences

**Positive**
- Genesis FX is already in the maintainer's network (Discord referrals are an active growth channel — see `jetlag-trading-room.md` in the project memory).
- TradeLocker's REST + WS combo gives us a clean integration path (ADR-0002).
- Demo accounts are free to create, lowering the barrier for casual testers to evaluate the product.
- One-broker scope keeps QA matrix small for v0.1.

**Negative**
- Single-broker dependency: a Genesis FX outage or change in T&Cs takes the product offline. Mitigated by NFR-3 graceful degradation and by keeping the broker layer abstracted behind `TradeLockerClient`.
- Some users want Apex / Topstep / Tradovate; we will lose conversions until v0.2 adds a second broker.
- If Genesis FX changes the TradeLocker server name, we have to update copy + helper-text in two places.

## Alternatives considered

| Option | Why rejected |
|--------|--------------|
| Apex Trader Funding (Tradovate) | Tradovate API is reportedly unreliable for Apex users (per LaT-PFN trading project notes); PickMyTrade is the workaround, which would add a third-party dependency. |
| Topstep | API surface is limited; webhook integration is recent and less battle-tested. |
| Oanda | Their own REST API is good, but Oanda's audience is FX-only and skews retail; doesn't match the prop-firm community we ship to. |
| Multi-broker on day 1 | Triples the QA and credential-storage surface for marginal short-term gain. |

## Implementation notes

- `User` carries `tradelocker_server` (free-text, defaults to whatever the user typed), so swapping or adding brokers later is a configuration change, not a schema migration.
- A future ADR will cover multi-broker support: how we model `BrokerType` in the schema and how the runner picks the right `BrokerClient` per user.
- The marketing site mentions "Genesis FX" explicitly — when a second broker is added, the home page, `LEGAL.md`, and the connect form copy all need a coordinated update.
