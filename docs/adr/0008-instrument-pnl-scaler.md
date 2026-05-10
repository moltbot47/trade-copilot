# ADR-0008 — Per-instrument lot-to-USD scaler for realized PnL

- **Status**: Accepted
- **Date**: 2026-05-10

## Context

The original PnL calculation in `position_monitor.py` applied a flat `100_000x` multiplier to every closed trade — the FX standard-lot convention (1 lot = 100,000 units of base currency). That convention is wrong for almost every non-FX instrument we trade:

- **Crypto** at TradeLocker: 1 lot = 1 BTC / 1 ETH (not 100k units).
- **Metals** (XAU, XAG): 1 lot = 100 troy ounces.
- **JPY pairs**: 1 lot is still 100k base, but pip math behaves differently because USDJPY quote scale is two decimals, not four.

The bug bit us on a 0.01-lot BTCUSD close. A $100 move in BTC produced `$100 × 0.01 × 100_000 = $100,000` reported PnL instead of the actual ~$1. That single phantom number poisoned downstream dashboards and the rolling win/loss accounting until it was scrubbed (see task #74).

The TradeLocker API does expose per-instrument contract size via `/trade/config`, but it is keyed by `tradableInstrumentId`, requires a token, and we don't currently fetch it on the close path. We also don't get a true exit fill price from TradeLocker without trade history, so PnL is already an estimate — we'd rather the estimate be in the right order of magnitude than perfectly looked up but blocked on a missing field.

## Decision

**Inline instrument-class detection in `_close_outcome` selects an explicit scaler based on the symbol string.**

```python
if any(c in sym for c in ("BTC","ETH","LTC","DOGE","SOL","XRP","BNB","ADA")):
    lot_multiplier = 1.0          # crypto: 1 lot = 1 base unit
elif "XAU" in sym or "XAG" in sym:
    lot_multiplier = 100.0        # metals: 1 lot = 100 oz
elif "JPY" in sym:
    lot_multiplier = 1_000.0      # JPY pairs
else:
    lot_multiplier = 100_000.0    # FX majors
pnl_usd = pnl_per_unit * qty * lot_multiplier
```

## Consequences

**Positive**
- Eliminates the 100,000x phantom-PnL bug for crypto closures.
- Order-of-magnitude correct for the four instrument classes we actually trade today.
- No new dependencies; no extra broker calls on the close path.
- Easy to audit — one function, one branch ladder.

**Negative**
- Requires manual maintenance every time we add a new instrument class (new crypto tickers, indices, exotic FX pairs).
- The crypto detection is substring-based; a future symbol containing "ETH" (e.g., `ETHEREUM` or a stock named `BETH`) could be misclassified.
- Realized PnL is still an estimate — exit price assumes TP or SL filled cleanly based on MFE/MAE, which is not an actual broker fill.

## Alternatives considered

| Option | Why rejected |
|--------|--------------|
| Trust broker-reported PnL | TradeLocker doesn't reliably emit a closed-position event with realized PnL on hedging-mode partial closes; we'd be back to estimation for half our exits. |
| Lookup table cached from `/trade/config` | Best long-term answer. Deferred because (a) it needs the user's token at the close path, (b) the cache invalidation story has to be designed alongside the broker reconciliation worker, (c) we wanted the crypto bug fixed same-day. |
| Pure pip-based PnL | Doesn't actually solve it — pip value still varies per instrument class. |

## Implementation notes

- Function: `RealizedPnLBuilder._close_outcome` — `backend/app/strategies/position_monitor.py:192–205`.
- Comment in code names the 2026-05-10 incident so the next person who reads the branch ladder knows why it exists.
- Realized PnL also flows out via `_ws_publish_trade` to the dashboard event bus, so any future change to the scaler immediately moves the displayed dollar amounts — coordinate with frontend.
- **Follow-up**: replace inline detection with a lookup against a cached `/trade/config` map, keyed by `tradableInstrumentId`, once the broker reconciliation worker lands (see P3 task list).
