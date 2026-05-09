"use client";

import type { TradeOutcome } from "@/lib/types";

type Props = {
  trades: TradeOutcome[];
};

function formatHold(seconds: number): string {
  if (!seconds || seconds < 0) return "—";
  if (seconds < 60) return `${Math.round(seconds)}s`;
  if (seconds < 3600) return `${Math.round(seconds / 60)}m`;
  return `${(seconds / 3600).toFixed(1)}h`;
}

export default function OpenPositionsTable({ trades }: Props) {
  // Heuristic: open positions are entries without a closed_at timestamp
  // (or where closed_at is null/empty). For v1 we filter accordingly.
  const open = trades.filter((t) => !t.closed_at);

  if (open.length === 0) {
    return (
      <div className="dim" style={{ fontSize: "0.85rem" }}>
        no open positions
      </div>
    );
  }

  return (
    <div style={{ overflowX: "auto" }}>
      <table>
        <thead>
          <tr>
            <th>symbol</th>
            <th>side</th>
            <th>qty</th>
            <th>entry</th>
            <th>current pnl</th>
            <th>hold</th>
            <th>drift (σ)</th>
          </tr>
        </thead>
        <tbody>
          {open.map((t) => {
            const pnlColor =
              (t.pnl_usd ?? 0) >= 0 ? "var(--accent)" : "var(--danger)";
            const hold = t.opened_at
              ? (Date.now() - new Date(t.opened_at).getTime()) / 1000
              : 0;
            return (
              <tr key={t.id}>
                <td>{t.instrument}</td>
                <td
                  style={{
                    color:
                      t.side === "buy" ? "var(--accent)" : "var(--danger)",
                    textTransform: "uppercase",
                  }}
                >
                  {t.side}
                </td>
                <td>{t.qty}</td>
                <td className="mono" style={{ fontSize: "0.85rem" }}>
                  {t.entry_price?.toFixed(2)}
                </td>
                <td style={{ color: pnlColor }}>
                  {(t.pnl_usd ?? 0) >= 0 ? "+" : ""}$
                  {(t.pnl_usd ?? 0).toFixed(2)}
                </td>
                <td className="dim">{formatHold(hold)}</td>
                <td className="dim">
                  {t.forecast_drift !== null && t.forecast_drift !== undefined
                    ? t.forecast_drift.toFixed(2)
                    : "—"}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}
