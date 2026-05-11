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

  // On-demand "what does the model say right now?" analysis. Doesn't
  // start the runner — gives the user a live confidence + suggested
  // entry/SL/TP per instrument so they can decide whether to press START.
  type AnalyzeRow = Awaited<ReturnType<typeof api.analyzeStrategy>>["items"][number];
  type AnalyzeResult = Awaited<ReturnType<typeof api.analyzeStrategy>>;
  const [analysis, setAnalysis] = useState<AnalyzeResult | null>(null);
  const [analyzing, setAnalyzing] = useState(false);
  const [analyzeErr, setAnalyzeErr] = useState<string | null>(null);

  const handleAnalyze = async () => {
    setAnalyzing(true);
    setAnalyzeErr(null);
    try {
      const data = await api.analyzeStrategy(botId, timeframe);
      setAnalysis(data);
    } catch (err) {
      setAnalyzeErr((err as Error).message || "analyze failed");
    } finally {
      setAnalyzing(false);
    }
  };

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

          <button
            type="button"
            onClick={handleAnalyze}
            disabled={offline || analyzing}
            className="btn"
            title="Run a one-shot LaT-PFN analysis without starting the runner"
            style={{
              padding: "0.4rem 0.85rem",
              fontSize: "0.82rem",
              cursor: analyzing ? "wait" : "pointer",
            }}
          >
            {analyzing ? "thinking…" : "run analysis now"}
          </button>

          <StrategyControlButton
            isRunning={!!state?.is_running}
            onStart={handleStart}
            onStop={handleStop}
            disabled={offline || commandInFlight}
          />
        </div>
      </header>

      {/* On-demand analysis result */}
      {(analyzing || analysis || analyzeErr) && (
        <section
          className="card"
          aria-labelledby="analyze-heading"
          style={{ borderColor: "var(--accent-dim)" }}
        >
          <header
            style={{
              display: "flex",
              justifyContent: "space-between",
              alignItems: "baseline",
              marginBottom: "0.5rem",
              gap: "0.5rem",
            }}
          >
            <h2 id="analyze-heading" style={{ margin: 0 }} className="accent">
              {">"} live model view
            </h2>
            <span className="dim" style={{ fontSize: "0.78rem" }}>
              {analyzing
                ? "calling LaT-PFN…"
                : analysis
                  ? `${analysis.items.length} pairs · ${analysis.elapsed_ms}ms · threshold ${analysis.threshold.toFixed(2)}σ · R:R ${analysis.target_rr.toFixed(1)}:1 · appetite ${analysis.risk_appetite}`
                  : "—"}
            </span>
          </header>

          {analyzeErr && (
            <p className="danger" style={{ fontSize: "0.85rem" }}>
              {analyzeErr}
            </p>
          )}

          {analyzing && !analysis && (
            <div
              style={{
                display: "flex",
                alignItems: "center",
                gap: "0.6rem",
                padding: "0.5rem 0",
                fontSize: "0.88rem",
              }}
              className="dim"
            >
              <span className="accent" style={{ fontWeight: 700 }}>
                ●
              </span>
              pulling 240 bars + running LaT-PFN forecast per pair…
            </div>
          )}

          {analysis && analysis.items.length > 0 && (
            <div
              style={{
                fontFamily: "monospace",
                fontSize: "0.82rem",
                overflowX: "auto",
              }}
            >
              <div
                style={{
                  display: "grid",
                  gridTemplateColumns:
                    "minmax(80px,90px) 60px minmax(70px,90px) minmax(80px,1fr) minmax(80px,1fr) minmax(80px,1fr) 70px 70px",
                  gap: "0.5rem 0.8rem",
                  padding: "0.35rem 0.5rem",
                  borderBottom: "1px solid var(--accent-dim)",
                }}
                className="dim"
              >
                <span>SYMBOL</span>
                <span>SIDE</span>
                <span>CONF (σ)</span>
                <span>ENTRY</span>
                <span>STOP LOSS</span>
                <span>TAKE PROFIT</span>
                <span>TP %</span>
                <span>R:R</span>
              </div>
              {analysis.items.map((row: AnalyzeRow) => (
                <div
                  key={row.symbol}
                  style={{
                    display: "grid",
                    gridTemplateColumns:
                      "minmax(80px,90px) 60px minmax(70px,90px) minmax(80px,1fr) minmax(80px,1fr) minmax(80px,1fr) 70px 70px",
                    gap: "0.5rem 0.8rem",
                    padding: "0.4rem 0.5rem",
                    borderBottom: "1px solid rgba(0, 200, 83, 0.08)",
                    background: row.fits_threshold
                      ? "rgba(0, 200, 83, 0.04)"
                      : "transparent",
                  }}
                >
                  <span className="accent" style={{ fontWeight: 700 }}>
                    {row.symbol}
                  </span>
                  {row.error ? (
                    <span
                      style={{
                        gridColumn: "2 / span 7",
                        color: "var(--warn)",
                      }}
                    >
                      error: {row.error}
                    </span>
                  ) : (
                    <>
                      <span
                        style={{
                          color:
                            row.direction === "buy"
                              ? "var(--accent)"
                              : "var(--danger)",
                          fontWeight: 700,
                        }}
                      >
                        {(row.direction || "—").toUpperCase()}
                      </span>
                      <span
                        style={{
                          color: row.fits_threshold
                            ? "var(--accent)"
                            : "var(--dim)",
                          fontWeight: row.fits_threshold ? 700 : 400,
                        }}
                      >
                        {row.confidence?.toFixed(2)}σ
                        {row.fits_threshold ? " ✓" : ""}
                      </span>
                      <span>{row.entry?.toFixed(4)}</span>
                      <span style={{ color: "var(--danger)" }}>
                        {row.stop_loss?.toFixed(4)}
                      </span>
                      <span style={{ color: "var(--accent)" }}>
                        {row.take_profit?.toFixed(4)}
                      </span>
                      <span className="dim">
                        {row.tp_pct?.toFixed(2)}%
                      </span>
                      <span className="dim">{row.rr?.toFixed(2)}</span>
                    </>
                  )}
                </div>
              ))}
            </div>
          )}

          {analysis && (
            <p
              className="dim"
              style={{ fontSize: "0.75rem", marginTop: "0.5rem", marginBottom: 0 }}
            >
              Read-only · no orders placed · same math the bot runs every tick
              ·{" "}
              <span className="accent" style={{ fontWeight: 700 }}>
                ✓
              </span>{" "}
              rows beat the threshold and would fire if you press START.
            </p>
          )}
        </section>
      )}

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

      {/* First-time-user onboarding checklist — shown only if no state
          (never connected) OR no trades closed yet. Disappears once
          there's real activity. */}
      {!state?.is_running && recentTrades.length === 0 && (
        <section
          className="card"
          aria-label="Onboarding checklist"
          style={{ padding: "1rem 1.25rem", borderColor: "var(--accent-dim)" }}
        >
          <h2 style={{ marginTop: 0, marginBottom: "0.5rem" }} className="accent">
            {">"} getting started
          </h2>
          <p className="dim" style={{ marginTop: 0, fontSize: "0.88rem" }}>
            Four steps to take your first bot trade.
          </p>
          <ol style={{ paddingLeft: "1.4rem", marginTop: "0.5rem", lineHeight: 1.8, fontSize: "0.9rem" }}>
            <li>
              <a href="/connect">Connect your TradeLocker account</a>{" "}
              <span className="dim">— demo first, then live when ready</span>
            </li>
            <li>
              <a href="/settings/notifications">Configure Discord notifications</a>{" "}
              <span className="dim">— paste a webhook URL to see signals as they fire</span>
            </li>
            <li>
              <a href="/bots">Subscribe to a bot</a>{" "}
              <span className="dim">— the LaT-PFN Quant trader is a good starting point</span>
            </li>
            <li>
              Click <b>[start]</b> above{" "}
              <span className="dim">— the runner starts ticking and posts to Discord on every decision</span>
            </li>
          </ol>
          <p className="dim" style={{ fontSize: "0.82rem", marginBottom: 0 }}>
            We recommend enabling{" "}
            <a href="/settings/security">two-factor auth</a> on your account
            before going live.
          </p>
        </section>
      )}

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
