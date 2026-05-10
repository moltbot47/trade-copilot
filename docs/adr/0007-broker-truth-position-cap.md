# ADR-0007 — Broker is the source of truth for the position cap

- **Status**: Accepted
- **Date**: 2026-05-10

## Context

On 2026-05-10 we observed 8 broker positions open against a configured `max_concurrent_positions` cap of 3. Root cause analysis pointed at the cap check itself: it counted open cohorts in our database, but the 1m and 5m runners each maintain a separate cohort registry. Both runners could pass the `count_open_cohorts <= cap` check concurrently and each fire entries before the other had committed its rows.

The deeper problem is drift between our DB view of "what's open" and the broker's view. Manual broker positions, positions opened from another device, partial-fill races, and any window where a leg exists at the broker but not yet in our DB (or vice versa) all break the assumption that DB cohort count equals broker position count. Trading capital is at the broker — anything else is a guess.

## Decision

**The position cap is enforced against a live broker position count, fetched once per entry tick.**

- `RunnerService._count_broker_positions()` in `backend/app/strategies/runner.py` fetches `client.get_positions(...)` and returns `(total_count, by_tradable_id)`.
- The result is reused for two gates in `_tick`:
  1. Global cap (`broker_total >= cap` → `skip_position_cap`).
  2. Per-symbol exposure (`tradable_id in by_tid` → `skip_existing_position`).
- DB cohort counts are still used for management (trailing stops, partial closes), but never for the entry gate.

## Consequences

**Positive**
- Eliminates the 1m/5m race entirely — both runners see the same broker truth at the moment of decision.
- Catches positions opened outside Trade Copilot (manual fills, other tools) without any extra plumbing.
- One fetch per entry tick covers both cap and per-symbol exposure (no duplicate API calls).

**Negative**
- Extra REST call per entry tick adds latency (~100–300ms typical against TradeLocker).
- Broker downtime now blocks all entries. On `get_positions` exception we currently return `(0, {})` with a warning, which is the wrong direction (open-fail-open) for a safety gate. See follow-up note.
- Couples entry path to broker availability and rate limits.

## Alternatives considered

| Option | Why rejected |
|--------|--------------|
| Row-level locks on the cohorts table during entry | Still races against manual broker positions and broker-side fills. Requires SELECT FOR UPDATE on SQLite, which is awkward. |
| Per-symbol mutex in the Python process | Doesn't help if 1m/5m runners ever run in separate processes; same drift problem with broker reality remains. |
| Atomic INCR in Redis (cap as a counter) | Adds a new infra dependency and still doesn't reflect manual broker activity. |
| Webhook-driven position cache | Best long-term answer, but TradeLocker WS position events aren't reliably delivered today; we'd be back to polling as a fallback. |

## Implementation notes

- Function: `RunnerService._count_broker_positions` — `backend/app/strategies/runner.py:920–942`.
- Call site: `RunnerService._tick`, step "Broker truth ONE fetch for both position cap + exposure check" — `runner.py:1068–1106`.
- The skip decisions surface in tick logs as `skip_position_cap` and `skip_existing_position` for debugging.
- **Follow-up**: the `except Exception → return (0, {})` branch at `runner.py:935–937` should be revisited. A broker fetch failure currently lets entries through; safer default is `skip_broker_unreachable`.
