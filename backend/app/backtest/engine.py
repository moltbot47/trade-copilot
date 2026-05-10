"""BacktestEngine — walk-forward replay of OHLCV bars through a Strategy +
TradeManager state machine.

Pure in-memory: uses a fresh in-memory SQLite session so the real
production `TradeManager` runs unchanged. No broker calls, no HTTP —
deterministic and fast.

Pipeline per bar:
  1. Update intra-bar PnL high/low for any open cohorts (uses bar.high
     / bar.low to detect SL/TP hits that would have happened mid-bar).
  2. If an SL or TP was breached intra-bar, exit the cohort at the
     breach price (SL takes priority for conservative modelling — see
     constructor docstring).
  3. Call `strategy.on_bar(symbol, bars_so_far)` to emit a new entry
     signal. If a signal fires AND no cohort is currently open, open
     a new cohort at the next bar's open (the bar AFTER signal close)
     plus the configured spread.
  4. For each open cohort, call `TradeManager.evaluate(...)` against
     bar.close (the bar's official mark) and execute the returned
     command (scale_in / partial_close / modify_sl / exit_all) directly
     against in-memory state.

Spread modelling: `spread_ticks` is the spread in "ticks" where 1 tick
= $0.01 of price (matches the production TradeLocker symbol scale for
crypto pairs like BTCUSD). For other instruments, override by passing
a custom `tick_size`. Spread is applied on entry only — exits are
modelled at the breach price (SL/TP) or at bar close.

Result: a `BacktestResult` with full trade ledger, equity curve, and
metrics. See app.backtest.results.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Optional

import pandas as pd
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.database import Base
import app.db.models  # noqa: F401  (register models on Base)
from app.db.models import Bot, Cohort, CohortStatus, StrategyType, User
from app.strategies.base import StrategySignal
from app.strategies.momentum import compute_atr
from app.strategies.trade_manager import (
    TradeManager,
)

from app.backtest.results import BacktestResult

logger = logging.getLogger(__name__)


# Instrument lot multipliers — mirrors PositionMonitor's
# instrument-specific scaling so the backtest P&L matches what a live
# trade would book. Single source of truth: keep this dict in sync
# with app/strategies/position_monitor.py:192-205.
def _lot_multiplier(symbol: str) -> float:
    sym = (symbol or "").upper()
    if any(c in sym for c in ("BTC", "ETH", "LTC", "DOGE", "SOL", "XRP", "BNB", "ADA")):
        return 1.0  # crypto: 1 lot = 1 base unit
    if "XAU" in sym or "XAG" in sym:
        return 100.0  # metals
    if "JPY" in sym:
        return 1_000.0
    return 100_000.0  # FX majors default


@dataclass
class BacktestConfig:
    instrument: str
    lot: float = 0.01
    spread_ticks: int = 0
    tick_size: float = 0.01  # $0.01 per tick by default
    timeframe: str = "1m"
    # Minimum bars before the strategy is first invoked. Mirrors the
    # 16-bar minimum in LatPFNQuantStrategy.on_bar() but is overridable
    # so a custom strategy that needs more history can extend it.
    warmup_bars: int = 30
    # If True, ignore strategy.on_bar entry signals and only let the
    # manager state machine run on pre-seeded cohorts (useful for
    # testing the manager in isolation).
    skip_entries: bool = False


class _NullForecastClient:
    """Stand-in LaT-PFN client that returns a tunable mean/std.

    Used by tests that want to deterministically drive
    LatPFNQuantStrategy without hitting the real model server.
    """

    def __init__(self, drift_atr: float = 0.0, sigma_atr: float = 1.0) -> None:
        self.drift_atr = drift_atr
        self.sigma_atr = sigma_atr

    async def forecast(self, bars: pd.DataFrame, n_predict: int = 12) -> dict:
        current = float(bars["close"].iloc[-1])
        atr = compute_atr(bars, 14)
        mean_end = current + self.drift_atr * atr
        mean = [current] * (n_predict - 1) + [mean_end]
        std = [max(self.sigma_atr * atr, 1e-6)] * n_predict
        return {"mean": mean, "std": std, "current_price": current}


class BacktestEngine:
    """Replay a bar series through a strategy + TradeManager.

    Parameters
    ----------
    strategy:
        An object with an async `on_bar(symbol, bars)` method that
        returns a StrategySignal or None. Production strategies
        (LatPFNQuantStrategy, LatPFNMomentumStrategy) work directly.
    bars:
        OHLCV DataFrame with columns [open, high, low, close, volume],
        indexed by timestamp (ascending). At least `warmup_bars`+1
        rows recommended.
    instrument:
        Symbol string e.g. "BTCUSD". Drives lot-multiplier scaling.
    lot:
        Base lot size for entry. Defaults to 0.01.
    spread_ticks:
        Spread in ticks applied to entry fills only. 1 tick = $0.01
        of price by default (override via tick_size). Crypto pairs
        on TradeLocker quote 2 decimals → 1 tick = $0.01.
    tick_size:
        Price units per tick. Default $0.01.

    Notes
    -----
    SL/TP intra-bar modelling: if a bar's [low, high] range engulfs
    BOTH the SL and TP, we conservatively assume the SL is hit first
    (worst-case for the strategy). This is the standard convention
    for walk-forward backtests where bar-level resolution prevents
    knowing the actual fill order.
    """

    def __init__(
        self,
        strategy: Any,
        bars: pd.DataFrame,
        instrument: str,
        lot: float = 0.01,
        spread_ticks: int = 0,
        tick_size: float = 0.01,
        timeframe: str = "1m",
        warmup_bars: int = 30,
    ) -> None:
        if bars is None or len(bars) == 0:
            raise ValueError("bars must be a non-empty DataFrame")
        required = {"open", "high", "low", "close"}
        if not required.issubset(set(bars.columns)):
            raise ValueError(
                f"bars missing columns; required={required}, got={set(bars.columns)}"
            )
        self.strategy = strategy
        self.bars = bars.reset_index().rename(
            columns={bars.index.name or "index": "ts"}
        )
        # Ensure ts column exists; if index was unnamed pandas calls it "index"
        if "ts" not in self.bars.columns:
            self.bars["ts"] = pd.RangeIndex(start=0, stop=len(self.bars))
        self.config = BacktestConfig(
            instrument=instrument,
            lot=lot,
            spread_ticks=spread_ticks,
            tick_size=tick_size,
            timeframe=timeframe,
            warmup_bars=warmup_bars,
        )
        self.lot_multiplier = _lot_multiplier(instrument)

        # In-memory SQLite + TradeManager — gives us the real production
        # state machine without any refactor.
        self._engine = create_engine(
            "sqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
            future=True,
        )
        Base.metadata.create_all(bind=self._engine)
        Session = sessionmaker(
            bind=self._engine, autoflush=False, future=True
        )
        self._db = Session()
        bot = Bot(
            name="Backtest Bot",
            slug="backtest",
            description="",
            strategy_type=StrategyType.latpfn_quant,
            instruments_csv=instrument,
            webhook_secret="backtest",
        )
        user = User(
            email="backtest@local",
            hashed_password="x",
            tradelocker_acc_num="0",
            tradelocker_account_id="0",
        )
        self._db.add_all([bot, user])
        self._db.commit()
        self._db.refresh(bot)
        self._db.refresh(user)
        self._bot_id = bot.id
        self._user_id = user.id
        self.tm = TradeManager(
            self._db,
            bot_id=bot.id,
            user_id=user.id,
            timeframe=timeframe,
        )

        # Cumulative ledger
        self._trades: list[dict] = []
        self._equity_curve: list[tuple[str, float]] = []
        self._cum_pnl: float = 0.0

    # ---------- public API ----------

    def run(self) -> BacktestResult:
        """Synchronously run the full walk-forward replay."""
        return asyncio.run(self._run_async())

    async def _run_async(self) -> BacktestResult:
        bars = self.bars
        symbol = self.config.instrument
        warmup = self.config.warmup_bars

        pending_signal: Optional[StrategySignal] = None

        for i in range(len(bars)):
            bar = bars.iloc[i]
            ts = bar["ts"]
            close_px = float(bar["close"])
            high = float(bar["high"])
            low = float(bar["low"])
            open_px = float(bar["open"])

            # 1. Fill any pending signal at this bar's OPEN (next-bar
            #    fill convention — never look ahead). Spread applied.
            if pending_signal is not None:
                self._open_cohort_from_signal(pending_signal, fill_price=open_px)
                pending_signal = None

            # 2. Intra-bar SL/TP check on every open cohort. Use the
            #    bar's [low, high] envelope; if either trigger fires,
            #    close at that level. SL conservatively wins on a
            #    tie (both hit in same bar).
            self._check_intra_bar_exits(ts, high=high, low=low)

            # 3. Per-bar evaluate() against bar.close — runs the
            #    full scale_in / partial_close / trail / exit logic.
            self._evaluate_open_cohorts(ts, current_price=close_px)

            # 4. Update equity curve at bar close.
            self._equity_curve.append(
                (str(ts), round(self._cum_pnl, 6))
            )

            # 5. Strategy.on_bar — emit entry candidate for next bar.
            if (
                not self.config.skip_entries
                and i >= warmup
                and not self._has_open_cohort()
            ):
                window = bars.iloc[: i + 1].copy()
                # The strategy expects an OHLCV frame indexed by ts.
                window = window.set_index("ts")
                try:
                    sig = await self.strategy.on_bar(symbol, window)
                except Exception as exc:  # pragma: no cover — defensive
                    logger.warning("strategy.on_bar failed bar=%d: %s", i, exc)
                    sig = None
                if sig is not None:
                    pending_signal = sig

        # Build result
        result = BacktestResult(
            strategy=getattr(self.strategy, "name", "unknown"),
            symbol=symbol,
            bars_count=len(bars),
            start_ts=str(bars.iloc[0]["ts"]) if len(bars) else None,
            end_ts=str(bars.iloc[-1]["ts"]) if len(bars) else None,
            trades=self._trades,
            equity_curve=self._equity_curve,
            config={
                "instrument": self.config.instrument,
                "lot": self.config.lot,
                "spread_ticks": self.config.spread_ticks,
                "tick_size": self.config.tick_size,
                "timeframe": self.config.timeframe,
                "warmup_bars": self.config.warmup_bars,
                "lot_multiplier": self.lot_multiplier,
            },
        )
        result.compute_metrics()
        return result

    # ---------- private helpers ----------

    def _has_open_cohort(self) -> bool:
        rows = self.tm.list_open_cohorts(self.config.instrument)
        return any(c.status != CohortStatus.closed for c in rows)

    def _open_cohort_from_signal(
        self, sig: StrategySignal, fill_price: float
    ) -> Cohort:
        spread = self.config.spread_ticks * self.config.tick_size
        if sig.side == "buy":
            fill = fill_price + spread
        else:
            fill = fill_price - spread
        atr = float(sig.extra.get("atr") or 0.0)
        if atr <= 0:
            # Fall back to 1R distance from SL.
            atr = abs(sig.entry_price - sig.stop_loss)
            if atr <= 0:
                atr = max(fill_price * 0.001, 1e-6)
        cohort = self.tm.open_cohort(
            instrument=sig.symbol,
            side=sig.side,
            entry_price=fill,
            atr=atr,
            qty=sig.qty or self.config.lot,
            stop_loss=sig.stop_loss,
            take_profit=sig.take_profit,
            tl_position_id=f"BT-POS-{int(self._db.query(Cohort).count())}",
            tl_order_id=f"BT-ORD-{int(self._db.query(Cohort).count())}",
            forecast_drift=sig.forecast_drift,
            forecast_confidence=sig.forecast_confidence,
        )
        self._db.commit()
        return cohort

    def _check_intra_bar_exits(
        self, ts: Any, high: float, low: float
    ) -> None:
        """For every open cohort, see if SL or TP was breached in this bar.

        Conservative tie-breaking: if both SL and TP fall inside the
        bar's range, assume SL hit first.
        """
        for cohort in list(self.tm.list_open_cohorts(self.config.instrument)):
            if cohort.status == CohortStatus.closed:
                continue
            sl = float(cohort.current_stop)
            tp = float(cohort.initial_take_profit)
            exit_price: Optional[float] = None
            reason = ""
            if cohort.side == "buy":
                sl_hit = low <= sl
                tp_hit = high >= tp
                if sl_hit and tp_hit:
                    exit_price, reason = sl, "sl_hit_intrabar"
                elif sl_hit:
                    exit_price, reason = sl, "sl_hit_intrabar"
                elif tp_hit:
                    exit_price, reason = tp, "tp_final_hit_intrabar"
            else:  # sell
                sl_hit = high >= sl
                tp_hit = low <= tp
                if sl_hit and tp_hit:
                    exit_price, reason = sl, "sl_hit_intrabar"
                elif sl_hit:
                    exit_price, reason = sl, "sl_hit_intrabar"
                elif tp_hit:
                    exit_price, reason = tp, "tp_final_hit_intrabar"

            if exit_price is not None:
                self._close_cohort_and_record(
                    cohort, exit_price=exit_price, ts=ts, reason=reason
                )

    def _evaluate_open_cohorts(self, ts: Any, current_price: float) -> None:
        # Snapshot list since close_cohort mutates the open list.
        for cohort in list(self.tm.list_open_cohorts(self.config.instrument)):
            if cohort.status == CohortStatus.closed:
                continue
            # Forecast drift/confidence — backtest doesn't recompute
            # per-bar (would be expensive). Use the cohort's stored
            # forecast at entry as a stable proxy. Strategies that
            # need a live forecast view should call evaluate() with
            # zeros and rely on price action alone.
            drift = float(cohort.forecast_drift or 0.0)
            conf = float(cohort.forecast_confidence or 0.0)
            cmd = self.tm.evaluate(
                cohort,
                current_price=current_price,
                forecast_drift=drift,
                forecast_confidence=conf,
            )
            if cmd is None:
                continue
            self._execute_command(cohort, cmd, ts=ts, current_price=current_price)

    def _execute_command(
        self, cohort: Cohort, cmd: Any, ts: Any, current_price: float
    ) -> None:
        kind = cmd.kind
        if kind == "scale_in":
            # Mirror entry leg qty (the runner does the same).
            qty = cmd.qty or float(cohort.legs[0].qty)
            # Spread applied to scale-in fills too.
            spread = self.config.spread_ticks * self.config.tick_size
            fill = (
                current_price + spread
                if cohort.side == "buy"
                else current_price - spread
            )
            self.tm.add_scale_in_leg(
                cohort,
                entry_price=fill,
                qty=qty,
                stop_loss=cmd.new_stop or 0.0,
                tl_position_id=f"BT-LEG-{cohort.id}-{len(cohort.legs)}",
            )
            # Move existing entry-leg SL to break-even (mirrors runner).
            self.tm.update_stop(cohort, cohort.weighted_avg_entry, reason="scale_in_be")
            self._db.commit()

        elif kind == "partial_close":
            # Realized P&L on the closed half is computed inside
            # record_partial_close. We mirror its reasoning here for
            # the equity curve.
            avg = float(cohort.weighted_avg_entry)
            pnl_per_unit = (
                current_price - avg if cohort.side == "buy" else avg - current_price
            )
            partial_pnl = pnl_per_unit * float(cmd.qty) * self.lot_multiplier
            self.tm.record_partial_close(
                cohort, qty_closed=float(cmd.qty), close_price=current_price
            )
            if cmd.new_stop is not None:
                self.tm.update_stop(cohort, cmd.new_stop, reason="partial_be")
            self._db.commit()
            # Record as a partial trade event but do NOT close the cohort.
            self._cum_pnl += partial_pnl
            self._trades.append(
                {
                    "kind": "partial_close",
                    "cohort_id": cohort.id,
                    "side": cohort.side,
                    "qty": float(cmd.qty),
                    "entry_price": avg,
                    "exit_price": current_price,
                    "pnl_usd": round(partial_pnl, 6),
                    "r_multiple": round(
                        pnl_per_unit / max(abs(cohort.initial_entry_price - cohort.initial_stop_loss), 1e-9),
                        4,
                    ),
                    "ts": str(ts),
                    "reason": cmd.reason or "partial",
                }
            )

        elif kind == "modify_sl":
            if cmd.new_stop is not None:
                self.tm.update_stop(cohort, cmd.new_stop, reason=cmd.reason or "trail")
                self._db.commit()

        elif kind == "exit_all":
            self._close_cohort_and_record(
                cohort,
                exit_price=current_price,
                ts=ts,
                reason=cmd.reason or "exit_all",
            )

    def _close_cohort_and_record(
        self, cohort: Cohort, exit_price: float, ts: Any, reason: str
    ) -> None:
        avg = float(cohort.weighted_avg_entry)
        remaining_qty = max(
            0.0, float(cohort.total_qty) - float(cohort.closed_qty)
        )
        pnl_per_unit = (
            exit_price - avg if cohort.side == "buy" else avg - exit_price
        )
        final_pnl = pnl_per_unit * remaining_qty * self.lot_multiplier
        opened_at = cohort.opened_at or datetime.utcnow()
        # close_cohort writes to the DB session — must come AFTER we
        # snapshot the values we need.
        self.tm.close_cohort(cohort, close_price=exit_price, reason=reason)
        self._db.commit()
        closed_at = cohort.closed_at or datetime.utcnow()
        hold_seconds = max(
            0, int((closed_at - opened_at).total_seconds())
        )
        self._cum_pnl += final_pnl
        r_dist = max(
            abs(cohort.initial_entry_price - cohort.initial_stop_loss), 1e-9
        )
        self._trades.append(
            {
                "kind": "exit",
                "cohort_id": cohort.id,
                "side": cohort.side,
                "qty": remaining_qty,
                "entry_price": avg,
                "exit_price": exit_price,
                "pnl_usd": round(final_pnl, 6),
                "r_multiple": round(pnl_per_unit / r_dist, 4),
                "ts": str(ts),
                "reason": reason,
                "hold_seconds": hold_seconds,
            }
        )

    # ---------- cleanup ----------

    def close(self) -> None:
        try:
            self._db.close()
        finally:
            self._engine.dispose()

    def __enter__(self) -> "BacktestEngine":
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()
