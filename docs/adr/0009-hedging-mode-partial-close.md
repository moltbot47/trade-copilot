# ADR-0009 — Model partial closes as opposing `hedge_close` legs, not qty mutations

- **Status**: Accepted
- **Date**: 2026-05-10

## Context

TradeLocker operates in hedging mode for Genesis FX accounts: long and short positions on the same instrument coexist as separate position objects, and there is no "reduce qty on position X" REST endpoint. To close part of an existing long, the documented broker pattern is to **send an opposite-side market order for the partial quantity**, which the broker then nets against the open position(s) on that account.

That has a direct consequence for our domain model. A Trade Copilot "cohort" represents the lifecycle of one logical trade — entry, optional scale-ins, optional partial closes, final exit. If we modeled the partial close by mutating `entry_leg.qty` downward, we'd:

1. Lose the broker order id and fill price of the close leg.
2. Lose the auditability of when and why we partial-closed (the entry leg's `opened_at` would no longer correspond to that quantity ever existing).
3. Conflict with hedging-mode reality, because the broker has actually opened a new, opposing position object that we'd have to silently absorb.

## Decision

**Partial closes are recorded as new `CohortLeg` rows with `role="hedge_close"`, opposite `side`, and the closed quantity. The original entry leg's `qty` is never decremented.**

Cohort-level state tracks the net effect:

- `cohort.closed_qty += qty_closed`
- `cohort.realized_pnl += (close − avg_entry) × qty_closed` (sign-flipped for shorts)
- `cohort.status = partial`
- `cohort.last_action = "partial_close"`

Open-quantity math always computes from `total_qty − closed_qty`, or equivalently from `sum(leg.qty for leg in legs if leg.is_open and leg.role in ('entry','scale_in'))`.

## Consequences

**Positive**
- One-to-one mapping between `CohortLeg` rows and broker order ids — every broker action is auditable.
- Hedging-mode reality is preserved: each `hedge_close` leg carries its own `tradelocker_position_id` and `tradelocker_order_id`.
- Adding a second partial close at a later price doesn't need any retroactive rewriting; it's just another leg.

**Negative**
- `cohort.legs` grows over the lifetime of a trade; any code that walks legs must filter on `is_open` and/or `role` to avoid double-counting.
- `weighted_average_entry` / `open_qty` helpers must consistently exclude `hedge_close` legs, or use the explicit `cohort.closed_qty` / `cohort.total_qty` counters.
- Realized PnL is stored both on the leg (`leg.pnl_usd`) and aggregated on the cohort (`cohort.realized_pnl`); these can drift if not updated together.

## Alternatives considered

| Option | Why rejected |
|--------|--------------|
| Switch the account to netting mode | Loses the ability to run a long and short on the same symbol in different cohorts. We already use that for hedged grid-style entries on majors. Also changes the account configuration globally, surprising the user. |
| Mutate `entry_leg.qty` downward, no new leg | Loses the broker order id and close fill price; conflicts with the actual broker state (which has a new opposing position object). |
| Drop `hedge_close` legs entirely once cohort is closed | Breaks broker reconciliation — we'd no longer be able to answer "did this fill at the price we expected?" after the fact. |
| Single `position` table with `realized_at` column | Equivalent to the netting-mode model. Same problems plus an awkward partial state where part of a position is closed and part is open under one row. |

## Implementation notes

- Method: `TradeManager.record_partial_close` — `backend/app/strategies/trade_manager.py:265–302`.
- Sister method `record_scale_in` (same file) is the canonical example of "append leg, recompute aggregates" and should be kept structurally similar.
- `cohort.weighted_avg_entry` is computed via `weighted_average_entry(legs)` — which by convention excludes `hedge_close` legs. Any new helper that walks legs must respect that filter.
- Hedging-mode is currently implicit (we just assume Genesis FX accounts are hedging). When multi-broker support lands (see ADR-0006 follow-up), a `BrokerType.position_mode` field is the natural place to encode this so non-hedging brokers can take the simpler mutate-in-place path.
