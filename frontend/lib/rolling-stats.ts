/**
 * Client-side rolling stats from a TradeOutcome array.
 *
 * Why client-side: the backend's PerformanceSnapshot only refreshes every
 * 20 closed trades (the feedback-loop cadence). For a live dashboard we
 * want stats that twitch on every trade close, not every 20th close.
 *
 * Computation matches the server (app/strategies/performance_tracker.py):
 *   win_rate     = wins / total
 *   profit_factor = sum(wins.pnl) / abs(sum(losses.pnl))   (Infinity if no losses)
 *   avg_r        = mean(r_multiple)
 *   sharpe       = avg_r / stddev(r_multiple)              (annualization N/A — we use bar-level R)
 *   max_drawdown = peak-to-trough on cumulative pnl_usd, normalized by peak
 *   total_pnl    = sum(pnl_usd)
 *
 * If `trades` is empty, returns null — caller decides fallback.
 */
import type { TradeOutcome, PerformanceSnapshot } from "./types";

export function rollingStats(
  trades: TradeOutcome[],
  windowSize = 20,
): PerformanceSnapshot | null {
  if (!trades || trades.length === 0) return null;

  // Use the most recent N trades. recent_trades may already be ordered
  // newest-first or oldest-first depending on the API — normalize by sorting
  // by closed_at ascending so cumulative math is correct.
  const sorted = [...trades].sort(
    (a, b) => new Date(a.closed_at).getTime() - new Date(b.closed_at).getTime(),
  );
  const window = sorted.slice(-windowSize);
  if (window.length === 0) return null;

  let wins = 0;
  let winSum = 0;
  let lossSum = 0; // negative
  let pnlSum = 0;
  const rs: number[] = [];

  for (const t of window) {
    pnlSum += t.pnl_usd;
    rs.push(t.r_multiple);
    if (t.pnl_usd > 0) {
      wins += 1;
      winSum += t.pnl_usd;
    } else if (t.pnl_usd < 0) {
      lossSum += t.pnl_usd;
    }
  }

  const winRate = wins / window.length;

  let profitFactor: number;
  if (lossSum === 0) profitFactor = winSum > 0 ? Infinity : 0;
  else profitFactor = winSum / Math.abs(lossSum);

  const avgR = rs.reduce((a, b) => a + b, 0) / rs.length;
  let sharpe = 0;
  if (rs.length > 1) {
    const mean = avgR;
    const variance =
      rs.reduce((acc, r) => acc + (r - mean) * (r - mean), 0) / (rs.length - 1);
    const std = Math.sqrt(variance);
    sharpe = std > 0 ? mean / std : 0;
  }

  // Max drawdown on cumulative pnl
  let peak = 0;
  let cum = 0;
  let maxDD = 0;
  for (const t of window) {
    cum += t.pnl_usd;
    if (cum > peak) peak = cum;
    const dd = peak - cum;
    if (peak > 0 && dd / peak > maxDD) maxDD = dd / peak;
  }

  return {
    snapshot_at: window[window.length - 1].closed_at,
    window_size: window.length,
    win_rate: winRate,
    profit_factor: profitFactor,
    sharpe,
    avg_r: avgR,
    max_drawdown_pct: maxDD,
    total_pnl_usd: pnlSum,
    total_trades: window.length,
    threshold_after: 0,
    feedback_action: null,
  };
}

/**
 * Build an equity curve client-side from a TradeOutcome array.
 * One point per closed trade; cumulative_r and cumulative_pnl are running
 * sums in chronological order. This lets us drop the 30s REST poll.
 */
export function rollingEquityPoints(
  trades: TradeOutcome[],
): { ts: string; cumulative_r: number; cumulative_pnl: number }[] {
  if (!trades || trades.length === 0) return [];
  const sorted = [...trades].sort(
    (a, b) => new Date(a.closed_at).getTime() - new Date(b.closed_at).getTime(),
  );
  let cumR = 0;
  let cumPnl = 0;
  return sorted.map((t) => {
    cumR += t.r_multiple;
    cumPnl += t.pnl_usd;
    return {
      ts: t.closed_at,
      cumulative_r: cumR,
      cumulative_pnl: cumPnl,
    };
  });
}
