# Partner Strategy Module — Interface Spec

**Audience:** Strategy developers delivering a Python module to plug into Trade Copilot's StrategyRunner for live execution.

**Goal:** Let you ship strategy logic as a single file; the platform handles bar feed, broker execution, server-side stops, audit logging, kill switches, and partner transparency. Your code stays focused on signals, exit decisions, and cooldown — no infrastructure code required.

---

## 1. What the platform provides

You do **not** need to implement any of these:

| Concern | Handled by platform |
|---|---|
| 1-minute bar feed from TradeLocker | `BarFetcher` (WebSocket-driven, ≤200ms from broker tick) |
| Order placement | TradeLocker REST adapter with idempotent `client_order_id` |
| Server-side stop loss / trailing | Sent as part of the order; broker enforces |
| Position monitoring | `PositionMonitor` polls open positions, computes MAE/MFE |
| Kill switch | Global `panic_pause` halts all entries instantly |
| Circuit breaker | Auto-halt after N consecutive losses (per-user configurable) |
| Position cap | Broker-truth count (not DB cohort count) enforced server-side |
| Auth / token refresh | Transparent — every broker call is auth-aware |
| Audit log | Every signal, fill, and close written to `audit_log` table |
| Reconciliation | Hourly broker statement diff vs bot log |
| Partner webhook tee | See §5 — every event you emit gets HMAC-signed and POSTed to your endpoint |
| WS event fanout to dashboard | Live updates stream to `/ws` subscribers |

**What this means:** Your code only computes signals and decides exits. Everything else is wired up around it.

---

## 2. The contract

Your module must export a class that inherits from `Strategy` and implements:

```python
# velocity_spike.py
from typing import Optional
import pandas as pd

from app.strategies.base import Strategy, StrategySignal


class VelocitySpike(Strategy):
    name = "velocity_spike"     # required: unique per partner
    timeframe = "1m"            # required: must match runner timeframe
    partner_id = "vlad-2026"    # required: scopes audit + webhook to you

    def __init__(self, *, params: dict | None = None) -> None:
        # params come from StrategyState.config_json — tune without redeploy.
        self.params = params or {}
        # Hold your own state here: cooldown counter, last signal time, etc.
        self._cooldown_bars_remaining = 0

    async def on_bar(
        self,
        symbol: str,
        bars: pd.DataFrame,
    ) -> Optional[StrategySignal]:
        """Called once per 1-min bar close per symbol.

        Args:
            symbol: e.g. "NAS100" (or whatever instrument you support)
            bars:   pandas.DataFrame, last ~240 1-min bars.
                    Columns: [open, high, low, close, volume]
                    Index:   pandas.DatetimeIndex (UTC, ascending)

        Returns:
            StrategySignal if you want to enter a trade.
            None if no signal this bar (most bars).
        """
        # Cooldown enforcement is YOUR responsibility — runner does not
        # gate re-entries. Most strategies decrement a counter here.
        if self._cooldown_bars_remaining > 0:
            self._cooldown_bars_remaining -= 1
            return None

        # ... your logic ...
        if not your_entry_condition(bars):
            return None

        return StrategySignal(
            symbol=symbol,
            side="buy",
            entry_price=float(bars["close"].iloc[-1]),
            stop_loss=...,    # absolute price — broker holds this
            take_profit=...,  # absolute price — broker holds this
            qty=0.01,         # base lot; risk_engine scales per-user
            # Audit-required fields (see §6):
            expected_entry_price=float(bars["close"].iloc[-1]),
            hard_stop_distance_pts=50.0,
            early_stop_condition="momentum_stalls_3_bars",
            trailing_stop_distance_pts=3.0,
            # Free-form metadata:
            extra={"velocity_pct": ..., "stall_score": ...},
        )

    async def on_position_close(
        self,
        symbol: str,
        position: dict,
        outcome: dict,
    ) -> None:
        """Called after a position closes. Use this to enforce cooldown.

        Args:
            symbol:   "NAS100"
            position: broker's final position dict (raw TradeLocker JSON)
            outcome:  {pnl_pts, pnl_usd, exit_reason, hold_seconds, ...}

        Return value is ignored. Update your internal state here.
        """
        # Example: 3-bar cooldown after every close.
        self._cooldown_bars_remaining = self.params.get("cooldown_bars", 3)
```

Two methods. That's it.

---

## 3. The data types

### `pd.DataFrame` (bars input)
Standard pandas DataFrame. Columns are exactly `[open, high, low, close, volume]`, all `float64`. Index is `pd.DatetimeIndex` in UTC. Bars are guaranteed ascending and gap-free for the last ~240 bars (we backfill from broker history if needed).

Don't mutate `bars` — it's reused across symbols.

### `StrategySignal` (your return)

```python
@dataclass
class StrategySignal:
    # Required — broker fields
    symbol: str
    side: str              # "buy" | "sell"
    entry_price: float
    stop_loss: float       # absolute price; broker holds this
    take_profit: float     # absolute price; broker holds this
    qty: float = 0.01      # base lot; per-user risk scaling applied later

    # Required — audit fields (NEW; see §6)
    expected_entry_price: float = 0.0
    hard_stop_distance_pts: float = 0.0
    early_stop_condition: str = ""
    trailing_stop_distance_pts: float = 0.0

    # Optional — forecast metadata
    forecast_drift: float = 0.0
    forecast_confidence: float = 0.0
    threshold: float = 1.5

    # Optional — free-form
    extra: dict = field(default_factory=dict)
```

Set `take_profit = 0` if your strategy is trailing-stop only (no fixed TP).
Set `hard_stop_distance_pts = abs(entry_price - stop_loss)` so the audit can compare backtest vs real fill slippage at the same reference point.

---

## 4. Lifecycle

```
runner startup
  ├─→ load your module from configured strategy slug
  ├─→ instantiate YourStrategy(params=<from DB>)
  └─→ loop:
       on each 1-min bar close per symbol:
         ├─→ fetch bars (platform)
         ├─→ signal = await strategy.on_bar(symbol, bars)
         ├─→ if signal:
         │     ├─→ validate signal (sanity checks)
         │     ├─→ emit partner-webhook event "signal" (platform)
         │     ├─→ for each subscribed user:
         │     │     ├─→ place_order(...) with idempotent client_order_id
         │     │     ├─→ emit partner-webhook event "fill" (platform)
         │     │     └─→ track in PositionMonitor
         │     └─→ persist Signal + Executions to DB
         └─→ on each position close detected by PositionMonitor:
               ├─→ await strategy.on_position_close(symbol, position, outcome)
               ├─→ emit partner-webhook event "close" (platform)
               └─→ persist TradeOutcome to DB
  
runner shutdown
  └─→ stop polling, cancel task, flush logs
```

You only need to implement `on_bar` and `on_position_close`. Everything else is automatic.

---

## 5. Partner webhook hook (§emit_partner_event)

For independent verification, the platform tees every audit event to your own endpoint, signed with HMAC-SHA256.

### How it works

In your `StrategyState` config you supply two values:

```json
{
  "partner_webhook_url": "https://your-server.example.com/events",
  "partner_webhook_secret": "your-32-byte-random-hex"
}
```

The platform handles:
- HTTP POST with `Content-Type: application/json`
- Header `X-Signature: sha256=<hex>` (HMAC-SHA256 of body using your secret)
- Header `X-Timestamp: <unix-ms>` (replay protection)
- 3 retries with exponential backoff (1s, 4s, 16s)
- Logging every attempt server-side

### Events you receive

```json
// signal — strategy emitted a trade idea (before any user order placed)
{
  "event": "signal",
  "ts": "2026-06-08T14:32:15.234Z",
  "strategy": "velocity_spike",
  "symbol": "NAS100",
  "side": "buy",
  "expected_entry_price": 29234.5,
  "stop_loss": 29184.5,
  "take_profit": 0,
  "hard_stop_distance_pts": 50.0,
  "trailing_stop_distance_pts": 3.0,
  "early_stop_condition": "momentum_stalls_3_bars",
  "bar_close_ts": "2026-06-08T14:32:00.000Z",
  "bar_close_price": 29234.0,
  "signature_input_hash": "..."
}

// fill — broker confirmed an order fill for one user
{
  "event": "fill",
  "ts": "2026-06-08T14:32:15.521Z",
  "strategy": "velocity_spike",
  "account_id": "2163244",
  "user_pseudonym": "u_3f2a",
  "symbol": "NAS100",
  "side": "buy",
  "expected_entry_price": 29234.5,
  "actual_fill_price": 29234.8,
  "slippage_pts": 0.3,
  "latency_ms": 287,
  "broker_order_id": "tl_8847291",
  "broker_raw_response": { /* full TradeLocker JSON */ }
}

// close — position closed
{
  "event": "close",
  "ts": "2026-06-08T14:35:02.144Z",
  "strategy": "velocity_spike",
  "account_id": "2163244",
  "symbol": "NAS100",
  "exit_type": "trailing_stop",   // hard_stop | early_stop | trailing | manual
  "peak_price": 29257.0,           // best price reached during trade
  "expected_exit_price": 29254.0,  // where stop SHOULD have triggered
  "actual_exit_price": 29253.7,
  "exit_slippage_pts": 0.3,
  "strategy_pnl_pts": 22.5,
  "real_pnl_pts": 21.9,
  "hold_seconds": 167,
  "broker_raw_response": { /* full TradeLocker JSON */ }
}
```

### Verifying the signature on your side

```python
import hmac, hashlib

def verify(body_bytes: bytes, signature_header: str, secret: str) -> bool:
    expected = "sha256=" + hmac.new(
        secret.encode(),
        body_bytes,
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(expected, signature_header)
```

Reject any event where `X-Timestamp` is more than 5 minutes from current UTC — that's the replay window.

### Discord-only alternative

If you don't want to run a server, set `partner_webhook_url` to a Discord channel webhook URL. The platform detects Discord webhooks by URL prefix and formats events as readable messages instead of raw JSON. Zero infrastructure on your side, less queryable, fine for monitoring.

---

## 6. Audit metadata — why your signals must expose these fields

The 2-week audit phase compares **backtest expectation** to **live reality** per trade. For that comparison to be objective, your strategy has to publish (at signal-emit time) what it *expects* to happen. The platform compares to what *actually* happens via the broker.

Four fields are required on every `StrategySignal`:

| Field | Why | Example |
|---|---|---|
| `expected_entry_price` | What price your strategy assumed it would fill at | `bar_close + 0.0` for market-at-close, or `bar_close + 0.5` if you model entry slippage |
| `hard_stop_distance_pts` | The hard stop, in points from entry | `50.0` (so audit can compare actual loss vs intended stop) |
| `early_stop_condition` | Description of the "stall" exit condition, in plain English | `"momentum_stalls_3_bars"` |
| `trailing_stop_distance_pts` | Trailing stop distance from peak | `3.0` |

The audit pipeline computes:

```
entry_slippage   = actual_fill_price - expected_entry_price
exit_slippage    = actual_exit_price - expected_exit_price
strategy_pnl     = (intended_exit - intended_entry) * direction
real_pnl         = (actual_exit  - actual_entry)  * direction
edge_erosion_pts = strategy_pnl - real_pnl
```

If `edge_erosion_pts` averages more than the per-trade edge, the live edge is gone.

---

## 7. Cooldown enforcement (yours, not the runner's)

The runner does not enforce cooldown between trades — that's strategy logic. Track it inside your class:

```python
def __init__(self, *, params=None):
    self.params = params or {}
    self._cooldown_bars_remaining = 0

async def on_bar(self, symbol, bars):
    if self._cooldown_bars_remaining > 0:
        self._cooldown_bars_remaining -= 1
        return None
    # ... rest of your logic

async def on_position_close(self, symbol, position, outcome):
    self._cooldown_bars_remaining = self.params.get("cooldown_bars", 3)
```

`on_bar` is called once per bar even when you're in cooldown — so your counter decrements naturally.

---

## 8. Delivery format

### File layout

```
your_module/
├── __init__.py       (exports YourStrategyClass)
├── velocity_spike.py (your main strategy file)
└── README.md         (any deployment notes you want us to read)
```

We mount it at `backend/app/strategies/partner/your_module/`.

### Allowed dependencies

- Python stdlib
- `numpy`, `pandas`, `scipy` (already installed)
- `pandas-ta` (technical indicators, already installed)

No outbound network calls except via the platform's `emit_partner_event` hook. If you need an extra library, let us know — we'll evaluate it.

### IP protection options

Pick whichever fits your IP-protection comfort:

1. **Plain `.py`** — source visible. Fastest to debug, easiest to integrate. Recommended for the 2-week audit phase.
2. **Compiled `.pyc`** — bytecode only. Decompilable but discourages casual reading. Decent for post-audit production.
3. **Cython `.so`** — compiled C extension. Strong IP protection, no source visible. Use if/when we go to scale.
4. **External HTTP service** — you host the strategy on your VPS, we POST bars to you, you return signals. Most secure for IP, adds latency. Only if 1-3 are unacceptable.

The 4 audit fields in §6 must be exposed regardless of delivery format.

---

## 9. Velocity Spike — fill-in-the-blanks template

Copy this skeleton, replace the `...` blocks with your logic:

```python
# velocity_spike.py
from typing import Optional
import pandas as pd

from app.strategies.base import Strategy, StrategySignal


class VelocitySpike(Strategy):
    name = "velocity_spike"
    timeframe = "1m"
    partner_id = "vlad-2026"

    DEFAULT_PARAMS = {
        "velocity_window": 5,
        "exhaustion_lookback": 3,
        "hard_stop_pts": 50.0,
        "early_stop_bars": 3,
        "trailing_pts": 3.0,
        "cooldown_bars": 3,
        "session_start_utc_hour": 14,
        "session_end_utc_hour": 19,
    }

    def __init__(self, *, params: dict | None = None) -> None:
        self.params = {**self.DEFAULT_PARAMS, **(params or {})}
        self._cooldown_bars_remaining = 0

    async def on_bar(
        self, symbol: str, bars: pd.DataFrame
    ) -> Optional[StrategySignal]:
        # Cooldown gate
        if self._cooldown_bars_remaining > 0:
            self._cooldown_bars_remaining -= 1
            return None

        # Session gate (RTH 14-19 UTC = 9-2 ET)
        now = bars.index[-1]
        if not (
            self.params["session_start_utc_hour"]
            <= now.hour
            < self.params["session_end_utc_hour"]
        ):
            return None

        # ---- YOUR VELOCITY-SPIKE LOGIC GOES HERE ----
        velocity = ...      # compute velocity over velocity_window
        is_exhausted = ...  # detect exhaustion over exhaustion_lookback
        direction = ...     # "buy" or "sell" based on which extreme

        if not is_exhausted:
            return None
        # ---------------------------------------------

        bar_close = float(bars["close"].iloc[-1])
        hard_stop = (
            bar_close - self.params["hard_stop_pts"]
            if direction == "buy"
            else bar_close + self.params["hard_stop_pts"]
        )

        return StrategySignal(
            symbol=symbol,
            side=direction,
            entry_price=bar_close,
            stop_loss=hard_stop,
            take_profit=0.0,  # trailing-only
            qty=0.01,
            expected_entry_price=bar_close,
            hard_stop_distance_pts=self.params["hard_stop_pts"],
            early_stop_condition=(
                f"momentum_stalls_{self.params['early_stop_bars']}_bars"
            ),
            trailing_stop_distance_pts=self.params["trailing_pts"],
            extra={
                "velocity": float(velocity),
                "session_hour": now.hour,
            },
        )

    async def on_position_close(
        self, symbol: str, position: dict, outcome: dict
    ) -> None:
        self._cooldown_bars_remaining = self.params["cooldown_bars"]
```

---

## 10. Pre-flight test we run before live

Before any partner strategy module goes live on a TradeLocker account, we run:

1. **Static check** — module imports cleanly, `Strategy` subclass exists, required fields are set.
2. **Synthetic-data backtest** — your `on_bar` runs against 1,000 bars of synthetic 1-min data. We check it doesn't crash, doesn't loop infinitely, and respects cooldown.
3. **Replay on real bars** — your `on_bar` runs against the last 7 days of cached Nas100 1-min bars. We compare signal count to your backtest expectation (within ±20% is acceptable for the small sample).
4. **Audit-field validation** — every signal must have the 4 audit fields set and non-zero.

If all four pass, we deploy to a demo account. The 2-week audit starts there.

---

## 11. Out of scope (you do NOT implement)

Just to be explicit:

- ❌ No broker REST calls
- ❌ No `place_order` / `close_position`
- ❌ No DB access
- ❌ No bar fetching
- ❌ No WS / WebSocket code
- ❌ No retry logic (platform handles)
- ❌ No risk sizing (`risk_engine` scales `qty` per user)
- ❌ No persistence (your in-instance state is fine; runner respawns on restart)
- ❌ No outbound network (except `emit_partner_event` via platform)

If you find yourself writing any of these, stop and ask — almost certainly it's already done on our side.

---

## 12. Open questions to confirm with the platform team

Before you commit time, confirm:

1. **Instrument format** — Is it `NAS100`, `US100`, `NDX`, or something else on this TradeLocker broker? We'll send you the resolved name.
2. **Tick size** — Nas100 CFD tick on this broker (some are 0.1, some are 1.0). Affects your slippage math.
3. **Session boundaries** — UTC RTH window. Default 14:00-19:00 UTC but verify.
4. **`params` interface** — confirm what config you want exposed in the dashboard so you (or we) can tune without redeploying.

Send any clarifications to the Trade Copilot team before starting the module.

---

## 13. Quick reference

| You do | We do |
|---|---|
| Compute signals on bar close | Fetch bars, call `on_bar` |
| Decide exit conditions | Send orders, manage SL/TP server-side |
| Enforce cooldown in your state | Spawn one runner per bot+timeframe |
| Set the 4 audit fields | Compare backtest expectation vs real fill |
| Subclass `Strategy`, return `StrategySignal` | Webhook your endpoint with raw broker JSON |
| Implement `on_position_close` to update state | Compute MAE/MFE, trade outcome, P&L |

---

## 14. Versioning

This spec is **v1.0**, dated 2026-06-08. Breaking changes to the contract will be versioned (v1.1 for additions, v2.0 for breaking). The platform supports both versions during transition periods.

Partner strategy modules are expected to pin to the spec version they were authored against.

---

**Contact:** Send questions to the Trade Copilot team on Upwork or via the partner channel.
