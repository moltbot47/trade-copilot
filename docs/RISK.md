# Trade Copilot — Risk Engine

The risk engine sits between the webhook receiver and the broker adapter. Every signal is filtered, sized, and (sometimes) rejected before any order leaves the server.

## Pipeline

```
TradingView -> /api/webhooks/tradingview -> RiskEngine -> Broker (TradeLocker)
                                                |
                                                +-- DB log + Discord notify
```

## Aggression Levels

User selects one in the dashboard. The selection multiplies the strategy's `base_lot_size`.

| Level         | Multiplier | Per-trade risk target | Notes                                  |
|---------------|------------|------------------------|----------------------------------------|
| Conservative  | 0.5x       | ~0.5% account          | Small starts. Recommended for first month. |
| Balanced      | 1.0x       | ~1.0% account          | Default.                                |
| Aggressive    | 1.75x      | ~1.75% account         | Only after a profitable balanced month. |

These are **multipliers**, not promises. Final size is also capped by the broker's max-position rule and the per-trade risk %.

## Hard Limits (always enforced)

- **Max daily loss %**: cumulative realized loss for the trading day. When hit, all open positions are closed and new signals are blocked until the next session.
- **Max open positions**: per-bot and per-account.
- **Allowed instruments**: signals on disallowed symbols are dropped silently with a log entry.
- **Cool-down after stop**: configurable minutes before the same bot can re-fire.
- **Kill switch**: dashboard toggle that immediately blocks all new orders system-wide.

## Sizing Math

```
position_size = base_lot_size
              * aggression_multiplier
              * min(1.0, max_per_trade_risk_pct * equity / risk_per_unit)
```

`risk_per_unit` is `abs(entry_price - stop_loss)`. If a signal lacks a stop, the engine rejects it.

## Why We Don't Promise Returns

- Past backtests are not future results.
- Slippage, gaps, broker outages, and your own panic-clicks compound losses.
- Educational platform: ship the tools, you own the outcomes.
- See `LEGAL.md`.

## Prop Firm Warning

**Most prop firms (FTMO, Topstep, Apex, MyForexFunds, etc.) prohibit third-party auto-trading and copy-trading.** Running Trade Copilot on a prop account can lead to:

- Account termination
- Forfeit of profits and fees
- Permanent ban from the firm

Use Trade Copilot only on:

- Your own personal live broker account
- A demo/paper account
- Genesis FX TradeLocker accounts (referral `DURBUT503`) where it's permitted by the broker's TOS

Always read your broker's terms before connecting. We do not provide legal cover.
