"""BacktestResult — metrics + serialization for a single backtest run.

The engine emits one BacktestResult per `run()` call. Aggregates trade
ledger, equity curve, and the headline metrics most-asked of any
quant strategy review: win rate, profit factor, Sharpe, Sortino,
max drawdown, total P&L.
"""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from typing import Any, Optional


@dataclass
class BacktestResult:
    """Outcome of a single backtest run."""

    strategy: str
    symbol: str
    bars_count: int
    start_ts: Optional[str] = None
    end_ts: Optional[str] = None

    # Ledger
    trades: list[dict] = field(default_factory=list)
    # List of (timestamp_iso, cumulative_pnl_usd)
    equity_curve: list[tuple[str, float]] = field(default_factory=list)

    # Headline metrics — populated by .compute_metrics()
    metrics: dict[str, float] = field(default_factory=dict)

    # Backtest configuration echoed back for traceability
    config: dict[str, Any] = field(default_factory=dict)

    # ---------- post-run metric computation ----------

    def compute_metrics(self, periods_per_year: int = 252 * 1440) -> None:
        """Populate `self.metrics` from the trade ledger + equity curve.

        `periods_per_year` defaults to 252 trading days * 1440 minutes
        (the per-bar count for a 1m strategy). Override for other
        timeframes if a more precise Sharpe annualization is wanted.
        """
        trades = self.trades or []
        n = len(trades)
        wins = [t for t in trades if (t.get("pnl_usd") or 0.0) > 0]
        losses = [t for t in trades if (t.get("pnl_usd") or 0.0) < 0]
        total_pnl = sum(float(t.get("pnl_usd") or 0.0) for t in trades)
        gross_profit = sum(float(t["pnl_usd"]) for t in wins) if wins else 0.0
        gross_loss = abs(sum(float(t["pnl_usd"]) for t in losses)) if losses else 0.0
        win_rate = (len(wins) / n) if n else 0.0

        # Profit factor: gross_profit / gross_loss. Guard against /0:
        # if there are no losses but wins exist, use math.inf (callers
        # serialize that to "inf" or large-number — done in to_dict).
        if gross_loss > 1e-12:
            profit_factor = gross_profit / gross_loss
        elif gross_profit > 0:
            profit_factor = math.inf
        else:
            profit_factor = 0.0

        # R-multiple stats (the strategy's own scaled risk units)
        rs = [float(t.get("r_multiple") or 0.0) for t in trades]
        avg_r = sum(rs) / len(rs) if rs else 0.0

        # Sharpe + Sortino on the equity-curve delta series. This is
        # bar-resolution returns; periods_per_year scales accordingly.
        returns = self._equity_returns()
        sharpe = self._sharpe(returns, periods_per_year)
        sortino = self._sortino(returns, periods_per_year)

        max_dd = self._max_drawdown_pct()

        self.metrics = {
            "total_trades": float(n),
            "wins": float(len(wins)),
            "losses": float(len(losses)),
            "win_rate": round(win_rate, 6),
            "profit_factor": (
                round(profit_factor, 4) if math.isfinite(profit_factor) else profit_factor
            ),
            "total_pnl_usd": round(total_pnl, 4),
            "avg_r": round(avg_r, 4),
            "gross_profit": round(gross_profit, 4),
            "gross_loss": round(gross_loss, 4),
            "sharpe": round(sharpe, 4) if math.isfinite(sharpe) else 0.0,
            "sortino": round(sortino, 4) if math.isfinite(sortino) else 0.0,
            "max_drawdown_pct": round(max_dd, 4),
        }

    def _equity_returns(self) -> list[float]:
        if len(self.equity_curve) < 2:
            return []
        eq = [float(p[1]) for p in self.equity_curve]
        rets: list[float] = []
        for i in range(1, len(eq)):
            rets.append(eq[i] - eq[i - 1])
        return rets

    @staticmethod
    def _sharpe(returns: list[float], periods_per_year: int) -> float:
        if not returns:
            return 0.0
        mean = sum(returns) / len(returns)
        var = sum((r - mean) ** 2 for r in returns) / max(len(returns) - 1, 1)
        std = math.sqrt(var) if var > 0 else 0.0
        if std < 1e-12:
            return 0.0
        return (mean / std) * math.sqrt(periods_per_year)

    @staticmethod
    def _sortino(returns: list[float], periods_per_year: int) -> float:
        if not returns:
            return 0.0
        mean = sum(returns) / len(returns)
        downside = [r for r in returns if r < 0]
        if not downside:
            return math.inf if mean > 0 else 0.0
        # Population downside deviation (vs target=0)
        var = sum(r * r for r in downside) / len(downside)
        std = math.sqrt(var) if var > 0 else 0.0
        if std < 1e-12:
            return 0.0
        return (mean / std) * math.sqrt(periods_per_year)

    def _max_drawdown_pct(self) -> float:
        """Max drawdown as a percentage of peak cumulative P&L.

        Since we operate on cumulative P&L (not balance), we treat the
        starting point as 1.0 + cumulative_pnl/initial_balance. To keep
        the metric agnostic of balance, we report drawdown in absolute
        dollars relative to peak — if peak <= 0 we return drawdown
        relative to the absolute peak magnitude (so a $-100 peak with
        a -$200 trough is a 100% drawdown by that metric).
        """
        if not self.equity_curve:
            return 0.0
        peak = float("-inf")
        max_dd = 0.0
        for _, eq in self.equity_curve:
            eq_f = float(eq)
            if eq_f > peak:
                peak = eq_f
            dd = peak - eq_f
            # Normalize: if peak is positive, % of peak; else absolute / max(|peak|, 1)
            denom = peak if peak > 1e-9 else max(abs(peak), 1.0)
            dd_pct = dd / denom if denom > 1e-9 else 0.0
            if dd_pct > max_dd:
                max_dd = dd_pct
        return max_dd

    # ---------- serialization ----------

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        # Replace math.inf in metrics (not JSON-safe) with a sentinel
        m = d.get("metrics", {})
        for k, v in list(m.items()):
            if isinstance(v, float) and not math.isfinite(v):
                m[k] = "inf" if v > 0 else "-inf"
        return d

    def to_markdown(self) -> str:
        m = self.metrics or {}
        lines = [
            f"# Backtest Report — {self.strategy} on {self.symbol}",
            "",
            f"- Bars: {self.bars_count}",
            f"- Window: {self.start_ts or 'n/a'} → {self.end_ts or 'n/a'}",
            "",
            "## Headline metrics",
            "",
            "| Metric | Value |",
            "|---|---|",
            f"| Total trades | {int(m.get('total_trades', 0))} |",
            f"| Wins / Losses | {int(m.get('wins', 0))} / {int(m.get('losses', 0))} |",
            f"| Win rate | {m.get('win_rate', 0.0):.2%} |",
            f"| Profit factor | {self._fmt(m.get('profit_factor', 0.0))} |",
            f"| Total P&L (USD) | ${m.get('total_pnl_usd', 0.0):.2f} |",
            f"| Avg R | {m.get('avg_r', 0.0):.3f} |",
            f"| Sharpe | {m.get('sharpe', 0.0):.3f} |",
            f"| Sortino | {self._fmt(m.get('sortino', 0.0))} |",
            f"| Max drawdown | {m.get('max_drawdown_pct', 0.0):.2%} |",
            "",
            "## Config",
            "",
        ]
        for k, v in (self.config or {}).items():
            lines.append(f"- **{k}**: `{v}`")
        if not self.config:
            lines.append("- (default)")
        return "\n".join(lines)

    @staticmethod
    def _fmt(v: Any) -> str:
        if isinstance(v, float):
            if not math.isfinite(v):
                return "inf" if v > 0 else "-inf"
            return f"{v:.3f}"
        return str(v)
