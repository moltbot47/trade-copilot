# ADR-0005 — LaT-PFN as the strategy brain for the momentum bot

- **Status**: Accepted
- **Date**: 2026-05-08

## Context

The catalog has three "classical" strategy bots (ORB Breakout, Squeeze Momentum, Stoch Hook Reversal) that consume signals from TradingView Pine scripts. We also wanted a self-contained, "model-driven" bot that could trade directionally without a human-tuned indicator — a differentiator and a research vehicle.

The maintainer has prior work with **LaT-PFN** (Latent-Time Prior-Fitted Network), an open-source zero-shot time-series forecaster (`Lat-PFN` repo). It produces a forecast distribution (mean + σ) over a future horizon given a context window, with no per-symbol training. Inference is CPU-tractable on the maintainer's Intel Mac (1.5–4 s per call at the production sequence length).

Alternatives considered: train a vendor-specific tiny model per symbol (high ops cost), use a classical statistical filter (Kalman / EMA crossover — no edge), or stub the bot until a paid model exists.

## Decision

The fourth bot, **`StrategyType.latpfn_momentum`**, uses LaT-PFN as its forecasting brain.

- The runner (`app/strategies/runner.py`) polls OHLCV bars via `data_feed.py`, calls `latpfn_client.py` for a forecast, and triggers a buy/sell when the forecast's directional drift exceeds the bot's current `confidence_threshold` (σ units).
- The threshold is **auto-tuned** by the feedback adjuster (`app/strategies/feedback.py`) every 20 closed trades based on rolling win rate, profit factor, and drawdown.
- The model runs as an HTTP service (cloud or `latpfn_endpoint=…` parameter on `/api/strategy/start`) — keeping the FastAPI process responsive.
- Inference timeout is bounded; on failure, the runner skips the bar and increments an error counter rather than blocking.

## Consequences

**Positive**
- Zero per-symbol training cost — the same model serves any instrument the user enables.
- Self-correcting: the feedback loop tightens the threshold when win rate drops below target, and pauses the runner via `StrategyState.paused_until` if drawdown exceeds the limit.
- Research-friendly: every closed trade carries `forecast_drift`, `forecast_confidence`, and `threshold_at_entry`, giving us post-mortem-ready data for free.
- Differentiator vs. competing dashboards that ship indicator-only bots.

**Negative**
- 1.5–4 s inference is acceptable on 1m bars but rules out tick-level scalping. Documented as a constraint (C-2).
- Model output requires careful unwrapping (the API returns dicts keyed `"input"`; the V_normalization constants are hand-coded in `latpfn_client.py`). Future LaT-PFN releases may break this.
- CPU inference at ≥10 concurrent users hits a wall; production deploy must move LaT-PFN to a GPU pod or batch requests.
- We inherit the open-source model's failure modes — flat forecasts in low-volatility regimes, occasional NaN outputs handled with skip-and-log.

## Alternatives considered

| Option | Why rejected |
|--------|--------------|
| Per-symbol classical filter (e.g., MACD crossover) | No new edge over the existing indicator bots. |
| Prophet / ARIMA | Trained per series, slow to adapt to regime change, no "zero-shot" benefit. |
| Closed vendor LLM-based signals | Latency, cost-per-call, vendor lock-in, regulatory color (advice-adjacent). |
| Defer the model bot | Loses the differentiator; reduces the catalog to 3 indicator bots. |

## Implementation notes

- `app/strategies/latpfn_client.py` is a thin adapter — it can be swapped for a different forecaster without touching the runner.
- The runner is keyed by `(bot_id, timeframe)`; multiple timeframes per bot are supported via separate runners.
- Memory mode flag is exposed as `latpfn_endpoint` on the `/strategy/start` request — empty means "use the configured default."
- See `LaT-PFN` repo notes in the project memory for ShapeConfig and the `n_context=16, n_sequence=240` defaults.
