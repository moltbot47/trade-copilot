"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import { useWebSocket } from "@/hooks/useWebSocket";
import type { StrategyTimeframe } from "@/lib/types";

const API_URL =
  process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000";

const RING_SIZE = 200; // most recent N kept in memory
const PAGE_SIZE = 100;

type TickRow = {
  id: number;
  tick_at: string;
  symbol: string;
  current_price: number | null;
  forecast_drift: number | null;
  forecast_std: number | null;
  forecast_confidence: number | null;
  threshold: number | null;
  decision: string;
  reason: string | null;
};

const DECISION_COLOR: Record<string, string> = {
  entry_buy: "var(--accent)",
  entry_sell: "var(--accent)",
  scale_in: "var(--warn)",
  partial_close: "var(--warn)",
  trail_sl: "var(--warn)",
  exit: "var(--text)",
  manage: "var(--text-dim)",
  skip_below_threshold: "var(--text-dim)",
  skip_existing_position: "var(--text-dim)",
  skip_paused: "var(--text-dim)",
  error: "var(--danger)",
};

function fmtTime(iso: string): string {
  // Backend timestamps are naive UTC ("2026-05-10T13:23:51"); JS treats
  // those as local time per spec, so we force UTC interpretation by
  // appending Z when no tz suffix is present, then format in local tz.
  const hasTz = /[zZ]|[+-]\d{2}:?\d{2}$/.test(iso);
  const d = new Date(hasTz ? iso : iso + "Z");
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleTimeString([], { hour12: false });
}

function fmtNum(v: number | null | undefined, digits = 2): string {
  if (v === null || v === undefined || !Number.isFinite(v)) return "—";
  return v.toFixed(digits);
}

export default function AnalysisLog({
  botId,
  timeframe,
}: {
  botId: number;
  timeframe: StrategyTimeframe;
}) {
  const [rows, setRows] = useState<TickRow[]>([]);
  const [loading, setLoading] = useState(true);
  const [paused, setPaused] = useState(false);
  const scrollRef = useRef<HTMLDivElement | null>(null);
  const ws = useWebSocket();

  // Initial REST fetch
  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    fetch(
      `${API_URL}/api/strategy/analysis?bot_id=${botId}&timeframe=${timeframe}&limit=${PAGE_SIZE}`,
      { credentials: "include" },
    )
      .then((r) => (r.ok ? r.json() : Promise.reject(new Error(`HTTP ${r.status}`))))
      .then((d) => {
        if (cancelled) return;
        // Backend returns newest-first. Reverse so oldest renders at top
        // and newest at the bottom (auto-scroll-pinned).
        const items: TickRow[] = (d.items ?? []).slice().reverse();
        setRows(items);
      })
      .catch(() => {
        /* ignore — show empty state */
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [botId, timeframe]);

  // WS subscription — append live ticks
  useEffect(() => {
    const off = ws.subscribe<TickRow>("analysis", (t) => {
      // Filter to our (bot, timeframe). The server scopes to user_id but
      // multiple bots/timeframes can stream into the same channel.
      const tt = t as TickRow & { bot_id?: number; timeframe?: string };
      if (tt.bot_id && tt.bot_id !== botId) return;
      if (tt.timeframe && tt.timeframe !== timeframe) return;
      setRows((prev) => {
        // Dedup by id
        if (prev.length && prev[prev.length - 1].id === t.id) return prev;
        const next = [...prev, t].slice(-RING_SIZE);
        return next;
      });
    });
    return off;
  }, [ws, botId, timeframe]);

  // Auto-scroll to bottom unless user has scrolled up
  useEffect(() => {
    if (paused) return;
    const el = scrollRef.current;
    if (!el) return;
    el.scrollTop = el.scrollHeight;
  }, [rows, paused]);

  const onScroll = (e: React.UIEvent<HTMLDivElement>) => {
    const el = e.currentTarget;
    const distFromBottom = el.scrollHeight - el.scrollTop - el.clientHeight;
    setPaused(distFromBottom > 32);
  };

  // Quick stats
  const stats = useMemo(() => {
    const total = rows.length;
    const entries = rows.filter((r) => r.decision.startsWith("entry_")).length;
    const skips = rows.filter((r) => r.decision.startsWith("skip_")).length;
    const errors = rows.filter((r) => r.decision === "error").length;
    return { total, entries, skips, errors };
  }, [rows]);

  return (
    <section
      className="card"
      style={{ display: "flex", flexDirection: "column", gap: "0.6rem" }}
      aria-label="Live strategy analysis"
    >
      <header
        style={{
          display: "flex",
          justifyContent: "space-between",
          alignItems: "baseline",
          gap: "0.5rem",
          flexWrap: "wrap",
        }}
      >
        <h2 style={{ margin: 0 }} className="accent">
          {">"} live analysis
        </h2>
        <div className="dim" style={{ fontSize: "0.78rem" }}>
          {stats.total} ticks · {stats.entries} entries ·{" "}
          {stats.skips} skips · {stats.errors} errors
          {paused && (
            <span style={{ color: "var(--warn)", marginLeft: "0.6rem" }}>
              ⏸ scroll-locked
            </span>
          )}
        </div>
      </header>

      {loading && rows.length === 0 ? (
        <p className="dim" style={{ fontSize: "0.85rem" }}>
          loading recent ticks...
        </p>
      ) : rows.length === 0 ? (
        <p className="dim" style={{ fontSize: "0.85rem" }}>
          waiting for the runner to tick — start the strategy above to see
          live decisions.
        </p>
      ) : (
        <div
          ref={scrollRef}
          onScroll={onScroll}
          role="log"
          aria-live="polite"
          aria-atomic="false"
          style={{
            maxHeight: 280,
            overflowY: "auto",
            border: "1px solid var(--border)",
            background: "var(--bg)",
            fontSize: "0.78rem",
            fontFamily:
              "'JetBrains Mono', ui-monospace, Menlo, Consolas, monospace",
            lineHeight: 1.5,
          }}
        >
          <table style={{ width: "100%", borderCollapse: "collapse" }}>
            <thead
              style={{
                position: "sticky",
                top: 0,
                background: "var(--bg-card)",
                zIndex: 1,
              }}
            >
              <tr style={{ textAlign: "left" }}>
                <th style={{ padding: "0.35rem 0.5rem" }}>time</th>
                <th style={{ padding: "0.35rem 0.5rem" }}>sym</th>
                <th style={{ padding: "0.35rem 0.5rem" }} title="last close">
                  px
                </th>
                <th
                  style={{ padding: "0.35rem 0.5rem" }}
                  title="forecast drift"
                >
                  drift
                </th>
                <th
                  style={{ padding: "0.35rem 0.5rem" }}
                  title="forecast confidence (σ)"
                >
                  conf
                </th>
                <th style={{ padding: "0.35rem 0.5rem" }} title="threshold">
                  thr
                </th>
                <th style={{ padding: "0.35rem 0.5rem" }}>decision</th>
                <th style={{ padding: "0.35rem 0.5rem" }}>reason</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((r) => (
                <tr
                  key={r.id}
                  style={{ borderTop: "1px dashed var(--border)" }}
                >
                  <td className="dim" style={{ padding: "0.25rem 0.5rem" }}>
                    {fmtTime(r.tick_at)}
                  </td>
                  <td style={{ padding: "0.25rem 0.5rem" }}>{r.symbol}</td>
                  <td style={{ padding: "0.25rem 0.5rem" }}>
                    {fmtNum(r.current_price, 2)}
                  </td>
                  <td style={{ padding: "0.25rem 0.5rem" }}>
                    {fmtNum(r.forecast_drift, 4)}
                  </td>
                  <td style={{ padding: "0.25rem 0.5rem" }}>
                    {fmtNum(r.forecast_confidence, 2)}
                  </td>
                  <td className="dim" style={{ padding: "0.25rem 0.5rem" }}>
                    {fmtNum(r.threshold, 2)}
                  </td>
                  <td
                    style={{
                      padding: "0.25rem 0.5rem",
                      color: DECISION_COLOR[r.decision] ?? "var(--text)",
                      fontWeight: r.decision.startsWith("entry_") ? 600 : 400,
                    }}
                  >
                    {r.decision}
                  </td>
                  <td className="dim" style={{ padding: "0.25rem 0.5rem" }}>
                    {r.reason ?? ""}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      <p className="dim" style={{ margin: 0, fontSize: "0.7rem" }}>
        Auto-archived after 10,000 entries per bot+timeframe. Live updates
        via WebSocket; reload to fetch the most recent {PAGE_SIZE}.
      </p>
    </section>
  );
}
