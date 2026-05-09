"use client";

import { useMemo, useState } from "react";
import type { TradeOutcome } from "@/lib/types";

type Props = {
  trades: TradeOutcome[];
  limit?: number;
};

type SortKey = "time" | "r" | "pnl";

function formatTime(iso: string): string {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return new Intl.DateTimeFormat(undefined, {
    month: "short",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  }).format(d);
}

export default function TradeLogTable({ trades, limit = 20 }: Props) {
  const [sortKey, setSortKey] = useState<SortKey>("time");

  const sorted = useMemo(() => {
    const closed = trades.filter((t) => !!t.closed_at);
    const arr = [...closed];
    arr.sort((a, b) => {
      if (sortKey === "time") {
        return (
          new Date(b.closed_at).getTime() - new Date(a.closed_at).getTime()
        );
      }
      if (sortKey === "r") return b.r_multiple - a.r_multiple;
      return b.pnl_usd - a.pnl_usd;
    });
    return arr.slice(0, limit);
  }, [trades, sortKey, limit]);

  if (sorted.length === 0) {
    return (
      <div className="dim" style={{ fontSize: "0.85rem" }}>
        no closed trades yet
      </div>
    );
  }

  const headerStyle = (key: SortKey): React.CSSProperties => ({
    cursor: "pointer",
    color: sortKey === key ? "var(--accent)" : "var(--text-dim)",
  });

  return (
    <div style={{ overflowX: "auto" }}>
      <table>
        <thead>
          <tr>
            <th style={headerStyle("time")} onClick={() => setSortKey("time")}>
              time {sortKey === "time" ? "↓" : ""}
            </th>
            <th>symbol</th>
            <th>side</th>
            <th>entry → exit</th>
            <th style={headerStyle("r")} onClick={() => setSortKey("r")}>
              R {sortKey === "r" ? "↓" : ""}
            </th>
            <th style={headerStyle("pnl")} onClick={() => setSortKey("pnl")}>
              pnl {sortKey === "pnl" ? "↓" : ""}
            </th>
            <th>conf</th>
          </tr>
        </thead>
        <tbody>
          {sorted.map((t) => {
            const rColor =
              t.r_multiple > 0
                ? "var(--accent)"
                : t.r_multiple < 0
                ? "var(--danger)"
                : "var(--text-dim)";
            const pnlColor =
              t.pnl_usd >= 0 ? "var(--accent)" : "var(--danger)";
            return (
              <tr key={t.id}>
                <td className="dim" style={{ fontSize: "0.78rem" }}>
                  {formatTime(t.closed_at)}
                </td>
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
                <td className="mono" style={{ fontSize: "0.82rem" }}>
                  {t.entry_price?.toFixed(2)} → {t.exit_price?.toFixed(2)}
                </td>
                <td style={{ color: rColor, fontWeight: 600 }}>
                  {t.r_multiple >= 0 ? "+" : ""}
                  {t.r_multiple.toFixed(2)}R
                </td>
                <td style={{ color: pnlColor }}>
                  {t.pnl_usd >= 0 ? "+" : ""}${t.pnl_usd.toFixed(2)}
                </td>
                <td className="dim" style={{ fontSize: "0.82rem" }}>
                  {t.threshold_at_entry !== null
                    ? `${t.threshold_at_entry.toFixed(2)}σ`
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
