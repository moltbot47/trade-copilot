# Trade Copilot — Strategy Reference

Three baseline Pine v5 strategies ship in `strategies/`. All are **educational templates** — tune to your instrument/timeframe before live use. Backtest stats live in `strategies/backtest_results.json` (placeholders until you run them yourself).

## How to Import

1. TradingView -> **Pine Editor**.
2. Open the `.pine` file in your editor, copy contents.
3. Paste into Pine Editor -> **Save** -> **Add to chart**.
4. Configure inputs (top of script).
5. Use **Strategy Tester** tab to backtest before adding alerts.

---

## 1. ORB Breakout — `orb_breakout.pine`

- **Pattern**: Opening Range Breakout. Define the high/low of the first N bars after session open, fade or follow the break.
- **Why it works**: Liquidity surge at the open creates the day's first directional commitment. A clean break past the range tends to extend during high-participation hours.
- **Best instruments**: Index futures (NQ, ES), large-cap equities, FX pairs around London/NY opens.
- **Timeframe**: 1m–15m. Default 5m, 30-min range.
- **Failure modes**:
  - Choppy days with no range expansion (Fed days, holidays).
  - Multiple false breaks on low-volume sessions.
  - Mean-reverting regimes (use a trend filter if needed).
- **bot_secret**: `orb-breakout`

---

## 2. Squeeze Momentum — `squeeze_momentum.pine`

- **Pattern**: TTM Squeeze. Bollinger Bands inside Keltner Channels = compression. Release + positive/negative momentum = directional thrust.
- **Why it works**: Volatility cycles between compression and expansion. Catching the expansion phase with momentum confirmation beats chasing.
- **Best instruments**: Trending crypto majors, gold, indices.
- **Timeframe**: 15m–4h. Higher timeframes filter noise.
- **Failure modes**:
  - Squeeze releases into news, then reverses.
  - Multiple consecutive squeezes (use only the first after a sustained on-period).
  - Range-bound sessions; momentum oscillates around zero.
- **bot_secret**: `squeeze-momentum`

---

## 3. Stoch Hook — `stoch_hook.pine`

- **Pattern**: Stochastic RSI %K crossing %D in oversold (long) or overbought (short) zones — mean-reversion "hook".
- **Why it works**: In ranges, stretched oscillators snap back. The cross is the trigger; a tight 5-bar swing stop keeps risk small.
- **Best instruments**: Range-bound FX (EURUSD, AUDUSD), low-vol equities.
- **Timeframe**: 15m–1h.
- **Failure modes**:
  - Strong trends (oscillator stays pinned, hook fails).
  - News spikes blow through the swing stop.
  - Disable during ATR expansion (volatility breakout regime).
- **bot_secret**: `stoch-hook`

---

## Backtest Stats

See `strategies/backtest_results.json`. Replace placeholder numbers with your own from TradingView's Strategy Tester. Sample stats are realistic (55–65% win rate, profit factor 1.2–1.4, max DD 7–12%) — not aspirational.

## Webhook Payload Format

All three scripts emit the same JSON shape; only `bot_secret` differs:

```json
{
  "bot_secret": "<strategy-id>",
  "instrument": "{{ticker}}",
  "side": "buy",
  "entry_price": {{close}},
  "stop_loss": {{strategy.position_avg_price}} - 50,
  "take_profit": {{strategy.position_avg_price}} + 75,
  "base_lot_size": 0.10
}
```

The backend uses `bot_secret` to route the order to the correct user-configured bot/risk profile.
