"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import { api, getUserEmail } from "@/lib/api";
import { useWebSocket } from "@/hooks/useWebSocket";
import EmailGate from "@/components/EmailGate";
import StrategyStatusBadge from "@/components/StrategyStatusBadge";
import StrategyControlButton from "@/components/StrategyControlButton";
import EquityCurve from "@/components/EquityCurve";
import StatsCard from "@/components/StatsCard";
import TradeLogTable from "@/components/TradeLogTable";
import FeedbackLogTable from "@/components/FeedbackLogTable";
import OpenPositionsTable from "@/components/OpenPositionsTable";
import ActivityLog from "@/components/ActivityLog";
import AnalysisLog from "@/components/AnalysisLog";
import type {
  StrategyState,
  StrategyTimeframe,
  PerformanceSnapshot,
  TradeOutcome,
  EquityPoint,
} from "@/lib/types";
import type { StrategyEvent, WsChannel } from "@/lib/ws-types";
import { rollingStats, rollingEquityPoints } from "@/lib/rolling-stats";

const DEFAULT_BOT_ID = 5; // LaT-PFN Quant Trader (most advanced — pyramiding + scale-out + trail SL)
const DEFAULT_SYMBOLS = ["BTCUSD", "ETHUSD"];  // crypto-only by default; quant fires aggressively on these

function formatRelative(iso: string | null | undefined): string {
  if (!iso) return "—";
  // Force UTC interpretation: backend serializes datetime.utcnow() without
  // a tz marker, which JS treats as local time → 5h drift on US-CDT.
  const hasTz = /[zZ]|[+-]\d{2}:?\d{2}$/.test(iso);
  const d = new Date(hasTz ? iso : iso + "Z");
  if (Number.isNaN(d.getTime())) return iso;
  const diff = Date.now() - d.getTime();
  if (diff < 60_000) return `${Math.max(0, Math.round(diff / 1000))}s ago`;
  if (diff < 3_600_000) return `${Math.round(diff / 60_000)}m ago`;
  return new Intl.DateTimeFormat(undefined, {
    month: "short",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  }).format(d);
}

function trendOf(curr: number, prev: number | null): "up" | "down" | "flat" {
  if (prev === null) return "flat";
  if (curr > prev + 1e-9) return "up";
  if (curr < prev - 1e-9) return "down";
  return "flat";
}

function profitFactorColor(pf: number): string {
  if (pf >= 1.5) return "var(--accent)";
  if (pf >= 1.0) return "var(--warn)";
  return "var(--danger)";
}

export default function StrategyPage() {
  const [timeframe, setTimeframe] = useState<StrategyTimeframe>("5m");
  const [state, setState] = useState<StrategyState | null>(null);
  const [perf, setPerf] = useState<PerformanceSnapshot | null>(null);
  const [recentTrades, setRecentTrades] = useState<TradeOutcome[]>([]);
  const [snapshots, setSnapshots] = useState<PerformanceSnapshot[]>([]);
  const [equity, setEquity] = useState<EquityPoint[]>([]);
  const [lastUpdated, setLastUpdated] = useState<Date | null>(null);
  const [offline, setOffline] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [tickNow, setTickNow] = useState<number>(Date.now());

  const prevPerfRef = useRef<PerformanceSnapshot | null>(null);

  // Bot resolution priority:
  //   1. ?bot=<slug> URL param (set by /bots Subscribe redirect)
  //   2. User's most recent subscription
  //   3. DEFAULT_BOT_ID (LaT-PFN Quant Trader)
  const [botId, setBotId] = useState<number>(DEFAULT_BOT_ID);
  const [botName, setBotName] = useState<string>("LaT-PFN Quant Trader");

  useEffect(() => {
    let cancelled = false;
    const resolveBotId = async () => {
      // 1. Read URL param
      const params =
        typeof window !== "undefined" ? new URLSearchParams(window.location.search) : null;
      const slugParam = params?.get("bot");

      try {
        // Always need bots list to map slug→id and get the display name
        const bots = await api.getBots();
        if (cancelled) return;

        if (slugParam) {
          const matched = bots.find((b) => b.slug === slugParam);
          if (matched) {
            setBotId(matched.id);
            setBotName(matched.name);
            return;
          }
        }
        // 2. Fall back to most recent subscription
        const subs = await api.getSubscriptions();
        if (cancelled) return;
        if (subs.length > 0) {
          const last = subs[subs.length - 1];
          const matched = bots.find((b) => b.id === last.bot_id);
          if (matched) {
            setBotId(matched.id);
            setBotName(matched.name);
            return;
          }
        }
        // 3. Default
        const def = bots.find((b) => b.id === DEFAULT_BOT_ID);
        if (def) setBotName(def.name);
      } catch {
        /* network errors: keep defaults */
      }
    };
    resolveBotId();
    return () => {
      cancelled = true;
    };
  }, []);

  const ws = useWebSocket();

  // Initial REST fetch — populates state before first WS push arrives.
  const refresh = async () => {
    try {
      const [statusRes, equityRes] = await Promise.allSettled([
        api.getStrategyStatus(botId, timeframe),
        api.getStrategyEquity(botId),
      ]);

      let backendOffline = false;

      if (statusRes.status === "fulfilled") {
        const r = statusRes.value;
        prevPerfRef.current = perf;
        setState(r.state);
        setPerf(r.performance);
        setRecentTrades(r.recent_trades || []);
        setSnapshots(r.recent_snapshots || (r.performance ? [r.performance] : []));
        setError(null);
      } else {
        const msg = (statusRes.reason as Error)?.message || "error";
        if (msg === "Backend offline") backendOffline = true;
        else setError(msg);
      }

      if (equityRes.status === "fulfilled") {
        setEquity(equityRes.value.points || []);
      } else {
        const msg = (equityRes.reason as Error)?.message || "error";
        if (msg === "Backend offline") backendOffline = true;
      }

      setOffline(backendOffline);
      setLastUpdated(new Date());
    } catch (err) {
      setError((err as Error).message);
    }
  };

  // Initial load when timeframe changes — single REST call, no polling.
  useEffect(() => {
    refresh();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [timeframe]);

  // Subscribe to the strategy channel for live state pushes.
  useEffect(() => {
    const channel: WsChannel = timeframe === "1m" ? "strategy:1m" : "strategy:5m";
    const off = ws.subscribe<StrategyEvent>(channel, (payload) => {
      prevPerfRef.current = perf;
      setState(payload.state);
      setPerf(payload.performance);
      setRecentTrades(payload.recent_trades || []);
      setSnapshots(
        payload.recent_snapshots || (payload.performance ? [payload.performance] : []),
      );
      setLastUpdated(new Date());
      setOffline(false);
    });
    return off;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [timeframe, ws.subscribe]);

  // WS connection status drives the offline banner.
  useEffect(() => {
    if (ws.status === "open") setOffline(false);
    else if (ws.status === "closed") setOffline(true);
  }, [ws.status]);

  // Equity is now derived client-side from recentTrades — instant updates
  // on every trade close, no REST polling. Server equity is still fetched
  // once on mount (in `refresh` above) as the initial backfill, but after
  // that we recompute as trades stream in via the strategy WS event.
  useEffect(() => {
    if (recentTrades.length > 0) {
      setEquity(rollingEquityPoints(recentTrades));
    }
  }, [recentTrades]);

  // Tick "Last updated Xs ago" every second
  useEffect(() => {
    const t = setInterval(() => setTickNow(Date.now()), 1000);
    return () => clearInterval(t);
  }, []);

  const secondsSinceUpdate = useMemo(() => {
    if (!lastUpdated) return null;
    return Math.max(0, Math.round((tickNow - lastUpdated.getTime()) / 1000));
  }, [lastUpdated, tickNow]);

  const lastUpdatedLabel =
    secondsSinceUpdate === null ? "—" : `${secondsSinceUpdate}s ago`;

  // Color-code freshness: green <15s, amber 15-60s, red >60s
  const freshnessColor =
    secondsSinceUpdate === null
      ? "var(--text-dim)"
      : secondsSinceUpdate < 15
      ? "var(--accent)"
      : secondsSinceUpdate < 60
      ? "var(--warn)"
      : "var(--danger)";

  // Dot class drives connectivity indicator next to "live ws stream"
  const wsDotClass =
    ws.status === "open"
      ? secondsSinceUpdate !== null && secondsSinceUpdate < 15
        ? "live-dot fresh"
        : secondsSinceUpdate !== null && secondsSinceUpdate < 60
        ? "live-dot stale"
        : "live-dot fresh"
      : ws.status === "closed"
      ? "live-dot dead"
      : "live-dot stale";

  const [commandInFlight, setCommandInFlight] = useState(false);

  const handleStart = async () => {
    const email = getUserEmail();
    const userEmails = email ? [email] : [];
    setCommandInFlight(true);
    try {
      let next: StrategyState;
      if (ws.status === "open") {
        next = await ws.command<StrategyState>("strategy.start", {
          bot_id: botId,
          timeframe,
          symbols: DEFAULT_SYMBOLS,
          user_emails: userEmails,
        });
      } else {
        next = await api.startStrategy(botId, timeframe, DEFAULT_SYMBOLS, userEmails);
      }
      setState(next);
      setError(null);
      // The WS strategy channel will push the next snapshot — no manual refresh needed.
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setCommandInFlight(false);
    }
  };

  const handleStop = async () => {
    setCommandInFlight(true);
    try {
      let next: StrategyState;
      if (ws.status === "open") {
        next = await ws.command<StrategyState>("strategy.stop", {
          bot_id: botId,
          timeframe,
        });
      } else {
        next = await api.stopStrategy(botId, timeframe);
      }
      setState(next);
      setError(null);
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setCommandInFlight(false);
    }
  };

  // Stats — prefer client-side rolling computation from recent_trades for
  // instant updates on every close. Falls back to the server snapshot when
  // the trade list is empty (e.g. server has older history we don't yet
  // have streamed in).
  const live = useMemo(
    () => rollingStats(recentTrades, 20),
    [recentTrades],
  );
  const effective = live ?? perf;
  const winRate = effective ? effective.win_rate : 0;
  const profitFactor = effective ? effective.profit_factor : 0;
  const sharpe = effective ? effective.sharpe : 0;
  const maxDD = effective ? effective.max_drawdown_pct : 0;

  const winRateTrend = effective
    ? trendOf(effective.win_rate, prevPerfRef.current?.win_rate ?? null)
    : null;

  const lastFeedback = snapshots.find((s) => !!s.feedback_action) || null;

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: "1.25rem" }}>
      <EmailGate />

      {/* Header */}
      <header
        style={{
          display: "flex",
          flexWrap: "wrap",
          gap: "1rem",
          alignItems: "center",
          justifyContent: "space-between",
        }}
      >
        <div>
          <div className="dim" style={{ fontSize: "0.85rem" }}>
            {">"} live strategy console
          </div>
          <h1 style={{ margin: "0.25rem 0" }}>
            <span className="accent">{botName}</span>
          </h1>
          <p className="dim" style={{ margin: 0, fontSize: "0.85rem" }}>
            zero-shot forecasting · self-tuning threshold ·{" "}
            <span className={wsDotClass} aria-hidden="true" />
            <span style={{ color: ws.status === "open" ? "var(--accent)" : "var(--text-dim)" }}>
              live ws stream {ws.status === "open" ? "(connected)" : `(${ws.status})`}
            </span>
          </p>
        </div>

        <div
          style={{
            display: "flex",
            alignItems: "center",
            gap: "1rem",
            flexWrap: "wrap",
          }}
        >
          {/* Timeframe selector */}
          <div
            style={{
              display: "inline-flex",
              border: "1px solid var(--border)",
            }}
          >
            {(["1m", "5m"] as StrategyTimeframe[]).map((tf) => (
              <button
                key={tf}
                onClick={() => setTimeframe(tf)}
                style={{
                  padding: "0.4rem 0.85rem",
                  background:
                    timeframe === tf ? "var(--accent)" : "transparent",
                  color: timeframe === tf ? "var(--bg)" : "var(--text-dim)",
                  border: "none",
                  cursor: "pointer",
                  fontWeight: 700,
                  fontSize: "0.82rem",
                  textTransform: "uppercase",
                  letterSpacing: "0.06em",
                }}
              >
                {tf}
              </button>
            ))}
          </div>

          <StrategyStatusBadge state={state} />

          <StrategyControlButton
            isRunning={!!state?.is_running}
            onStart={handleStart}
            onStop={handleStop}
            disabled={offline || commandInFlight}
          />
        </div>
      </header>

      {/* Backend offline banner */}
      {offline && (
        <div role="alert" className="card" style={{ borderColor: "var(--danger)" }}>
          <strong className="danger">backend offline.</strong>{" "}
          <span className="dim">
            Cannot reach{" "}
            {process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000"} — retrying every 10s.
          </span>
        </div>
      )}

      {error && !offline && (
        <div role="alert" className="card" style={{ borderColor: "var(--warn)" }}>
          <span className="warn">error:</span>{" "}
          <span className="dim">{error}</span>
        </div>
      )}

      {/* Threshold + last updated bar */}
      <section
        className="card"
        style={{
          display: "flex",
          flexWrap: "wrap",
          gap: "1.5rem",
          alignItems: "center",
          justifyContent: "space-between",
          padding: "0.7rem 1rem",
        }}
      >
        <div style={{ fontSize: "0.85rem" }}>
          <span className="dim">confidence threshold: </span>
          <span className="accent" style={{ fontWeight: 700 }}>
            {state?.confidence_threshold?.toFixed(2) ?? "—"}σ
          </span>
          {lastFeedback && (
            <span className="dim" style={{ marginLeft: "0.5rem" }}>
              · auto-adjusted {formatRelative(lastFeedback.snapshot_at)}
            </span>
          )}
        </div>
        <div
          style={{ fontSize: "0.78rem" }}
          role="status"
          aria-live="polite"
        >
          <span className="dim">last updated: </span>
          <span
            style={{
              color: freshnessColor,
              fontWeight: 600,
              transition: "color 240ms ease",
            }}
          >
            {lastUpdatedLabel}
          </span>
          {state?.last_signal_at && (
            <>
              <span className="dim">{"  ·  "}last signal: </span>
              <span className="dim">{formatRelative(state.last_signal_at)}</span>
            </>
          )}
        </div>
      </section>

      {/* Equity curve */}
      <section className="card">
        <h2 style={{ marginTop: 0 }} className="accent">
          {">"} equity curve (cumulative R)
        </h2>
        <EquityCurve points={equity} />
      </section>

      {/* Stats grid */}
      <section
        aria-live="polite"
        aria-atomic="false"
        aria-label="Live performance metrics"
        style={{
          display: "grid",
          gridTemplateColumns: "repeat(auto-fit, minmax(180px, 1fr))",
          gap: "1rem",
        }}
      >
        <StatsCard
          label="win rate"
          value={`${(winRate * 100).toFixed(1)}%`}
          trend={winRateTrend}
          subtitle={`rolling-${perf?.window_size ?? 20}`}
        />
        <StatsCard
          label="profit factor"
          value={profitFactor === Infinity ? "∞" : profitFactor.toFixed(2)}
          color={profitFactorColor(profitFactor)}
        />
        <StatsCard label="sharpe" value={sharpe.toFixed(2)} />
        <StatsCard
          label="max drawdown"
          value={`${(maxDD * 100).toFixed(1)}%`}
          color="var(--danger)"
        />
      </section>

      {/* Open positions */}
      <section className="card">
        <h2 style={{ marginTop: 0 }} className="accent">
          {">"} open positions
        </h2>
        <OpenPositionsTable trades={recentTrades} />
      </section>

      {/* Trade log */}
      <section className="card">
        <h2 style={{ marginTop: 0 }} className="accent">
          {">"} trade log (last 20)
        </h2>
        <TradeLogTable trades={recentTrades} limit={20} />
      </section>

      {/* Feedback loop */}
      <section className="card">
        <h2 style={{ marginTop: 0 }} className="accent">
          {">"} self-adjusting feedback loop
        </h2>
        <p className="dim" style={{ marginTop: 0, fontSize: "0.82rem" }}>
          last 5 performance snapshots + threshold adjustments
        </p>
        <FeedbackLogTable snapshots={snapshots} limit={5} />
      </section>

      {/* Activity log — live event stream from WS (Wave 5C) */}
      <AnalysisLog botId={botId} timeframe={timeframe} />

      <ActivityLog timeframe={timeframe} />
    </div>
  );
}
