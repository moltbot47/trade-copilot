"use client";

import type { Signal } from "@/lib/types";

function fmtTime(t: string): string {
  try {
    const d = new Date(t);
    if (isNaN(d.getTime())) return t;
    return d.toLocaleTimeString([], { hour12: false });
  } catch {
    return t;
  }
}

function statusColor(status: string): string {
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
          {signals.map((s) => (
            <tr key={s.id}>
              <td className="dim">{fmtTime(s.time)}</td>
              <td>{s.bot}</td>
              <td>{s.instrument}</td>
              <td
                style={{
                  color:
                    s.side === "BUY"
                      ? "var(--accent)"
                      : s.side === "SELL"
                      ? "var(--danger)"
                      : "var(--text)",
                }}
              >
                {s.side}
              </td>
              <td>{s.entry?.toFixed?.(2) ?? s.entry}</td>
              <td style={{ color: statusColor(s.status) }}>{s.status}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
