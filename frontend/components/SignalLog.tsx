"use client";

import type { Signal } from "@/lib/types";

function fmtTime(t: string | null | undefined): string {
  if (!t) return "—";
  try {
    const d = new Date(t);
    if (isNaN(d.getTime())) return String(t);
    return d.toLocaleTimeString([], { hour12: false });
  } catch {
    return String(t);
  }
}

function statusColor(status: string | null | undefined): string {
  if (!status || typeof status !== "string") return "var(--text)";
  const s = status.toLowerCase();
  if (s.includes("fill") || s.includes("open") || s === "filled") return "var(--accent)";
  if (s.includes("pend") || s.includes("queu")) return "var(--warn)";
  if (s.includes("rej") || s.includes("err") || s.includes("fail")) return "var(--danger)";
  return "var(--text)";
}

export default function SignalLog({ signals }: { signals: Signal[] }) {
  if (!signals || signals.length === 0) {
    return (
      <p className="dim" style={{ fontSize: "0.85rem" }}>
        no signals yet — subscribe to a bot to start
      </p>
    );
  }
  return (
    <div style={{ overflowX: "auto" }}>
      <table>
        <thead>
          <tr>
            <th>time</th>
            <th>bot</th>
            <th>instrument</th>
            <th>side</th>
            <th>entry</th>
            <th>status</th>
          </tr>
        </thead>
        <tbody>
          {signals.map((s, idx) => {
            // Defensive: position objects (different shape than Signal) may
            // flow through this component when the dashboard reuses the
            // signals state for positions. Tolerate missing fields rather
            // than crashing.
            const sideUpper =
              typeof s.side === "string" ? String(s.side).toUpperCase() : "";
            const sideColor =
              sideUpper === "BUY"
                ? "var(--accent)"
                : sideUpper === "SELL"
                  ? "var(--danger)"
                  : "var(--text)";
            // Coerce entry to a renderable string. Could be `entry` (Signal),
            // `avg_price` (Position object piggybacked through state), or
            // missing entirely.
            const rawEntry =
              (s as { entry?: number }).entry ??
              (s as { avg_price?: number }).avg_price;
            const entryStr: string =
              typeof rawEntry === "number" ? rawEntry.toFixed(2) : "—";
            return (
              <tr key={s.id ?? idx}>
                <td className="dim">{fmtTime(s.time)}</td>
                <td>{s.bot ?? ""}</td>
                <td>{s.instrument ?? ""}</td>
                <td style={{ color: sideColor }}>{sideUpper || ""}</td>
                <td>{entryStr}</td>
                <td style={{ color: statusColor(s.status) }}>{s.status ?? ""}</td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}
