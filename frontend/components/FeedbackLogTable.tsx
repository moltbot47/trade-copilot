"use client";

import type { PerformanceSnapshot } from "@/lib/types";

type Props = {
  snapshots: PerformanceSnapshot[];
  limit?: number;
};

function formatTime(iso: string): string {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return new Intl.DateTimeFormat(undefined, {
    month: "short",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  }).format(d);
}

const ACTION_COLOR: Record<string, string> = {
  tighten: "var(--warn)",
  loosen: "var(--accent)",
  hold: "var(--text-dim)",
  pause: "var(--danger)",
  warmup: "var(--text-dim)",
};

export default function FeedbackLogTable({ snapshots, limit = 5 }: Props) {
  const sorted = [...snapshots]
    .sort(
      (a, b) =>
        new Date(b.snapshot_at).getTime() - new Date(a.snapshot_at).getTime()
    )
    .slice(0, limit);

  if (sorted.length === 0) {
    return (
      <div className="dim" style={{ fontSize: "0.85rem" }}>
        no feedback events yet — runner needs at least one rolling window of
        closed trades
      </div>
    );
  }

  return (
    <div style={{ overflowX: "auto" }}>
      <table>
        <thead>
          <tr>
            <th>time</th>
            <th>win rate</th>
            <th>threshold</th>
            <th>action</th>
          </tr>
        </thead>
        <tbody>
          {sorted.map((s, i) => {
            const before = s.threshold_before;
            const after = s.threshold_after;
            const action = s.feedback_action || "—";
            const color = ACTION_COLOR[action] || "var(--text-dim)";
            return (
              <tr key={i}>
                <td className="dim" style={{ fontSize: "0.8rem" }}>
                  {formatTime(s.snapshot_at)}
                </td>
                <td>{(s.win_rate * 100).toFixed(1)}%</td>
                <td className="mono" style={{ fontSize: "0.82rem" }}>
                  {before !== undefined && before !== null
                    ? `${before.toFixed(2)}σ → `
                    : ""}
                  <span style={{ color: "var(--accent)" }}>
                    {after.toFixed(2)}σ
                  </span>
                </td>
                <td
                  style={{
                    color,
                    fontWeight: 600,
                    textTransform: "uppercase",
                    fontSize: "0.82rem",
                    letterSpacing: "0.05em",
                  }}
                >
                  {action}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}
