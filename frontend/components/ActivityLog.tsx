"use client";

/**
 * ActivityLog — Wave 5C
 *
 * Live event stream panel for /strategy. Subscribes to multiple WS channels
 * and renders each event as a single line. Auto-scrolls to bottom unless the
 * user has scrolled away (in which case a "↓ NEW" pill appears).
 *
 * No persistence — log is ephemeral and tied to the tab session. Trade history
 * is persisted server-side and surfaced via TradeLogTable elsewhere.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useWebSocket } from "@/hooks/useWebSocket";
import {
  type ActivityEntry,
  appendToRing,
  colorFor,
  diffSnapshotForActivity,
  entryFromPositions,
  entryFromSignal,
  entryFromTrade,
  formatClock,
  iconFor,
  labelFor,
  MAX_ACTIVITY_ENTRIES,
} from "@/lib/activity-log";
import type {
  PositionsEvent,
  SignalsEvent,
  StrategyEvent,
  TradesEvent,
  WsChannel,
} from "@/lib/ws-types";

type Props = {
  /** Currently-selected strategy timeframe — drives which strategy:Xm channel
   *  contributes snapshot diffs. Defaults to "5m". */
  timeframe?: "1m" | "5m";
  /** Override max entries (mostly for tests). */
  maxEntries?: number;
};

const SCROLL_BOTTOM_THRESHOLD_PX = 24;

export default function ActivityLog({
  timeframe = "5m",
  maxEntries = MAX_ACTIVITY_ENTRIES,
}: Props) {
  const ws = useWebSocket();
  const [entries, setEntries] = useState<ActivityEntry[]>([]);
  const [pinnedToBottom, setPinnedToBottom] = useState(true);
  const [hasNew, setHasNew] = useState(false);
  const scrollerRef = useRef<HTMLDivElement | null>(null);
  const prevSnapshotRef = useRef<StrategyEvent | null>(null);

  /* -------------------------- append helper -------------------------- */
  const appendEntries = useCallback(
    (toAdd: ActivityEntry[]) => {
      if (toAdd.length === 0) return;
      setEntries((prev) => appendToRing(prev, toAdd, maxEntries));
    },
    [maxEntries],
  );

  /* -------------------------- subscriptions -------------------------- */
  useEffect(() => {
    const offSignals = ws.subscribe<SignalsEvent>("signals", (payload) => {
      appendEntries([entryFromSignal(payload)]);
    });
    const offPositions = ws.subscribe<PositionsEvent>("positions", (payload) => {
      const e = entryFromPositions(payload);
      if (e) appendEntries([e]);
    });
    const offTrades = ws.subscribe<TradesEvent>("trades", (payload) => {
      appendEntries([entryFromTrade(payload)]);
    });
    const channel: WsChannel = timeframe === "1m" ? "strategy:1m" : "strategy:5m";
    const offStrategy = ws.subscribe<StrategyEvent>(channel, (payload) => {
      const diff = diffSnapshotForActivity(prevSnapshotRef.current, payload);
      prevSnapshotRef.current = payload;
      if (diff.length > 0) appendEntries(diff);
    });
    return () => {
      offSignals();
      offPositions();
      offTrades();
      offStrategy();
    };
  }, [ws.subscribe, timeframe, appendEntries]);

  /* -------------------------- auto-scroll ---------------------------- */
  // Whenever new entries arrive AND the user is pinned to bottom,
  // scroll to bottom on next layout.
  useEffect(() => {
    const el = scrollerRef.current;
    if (!el) return;
    if (pinnedToBottom) {
      el.scrollTop = el.scrollHeight;
      setHasNew(false);
    } else {
      // user has scrolled up — show "new" pill
      if (entries.length > 0) setHasNew(true);
    }
  }, [entries, pinnedToBottom]);

  const handleScroll = useCallback(() => {
    const el = scrollerRef.current;
    if (!el) return;
    const distFromBottom = el.scrollHeight - (el.scrollTop + el.clientHeight);
    const atBottom = distFromBottom <= SCROLL_BOTTOM_THRESHOLD_PX;
    setPinnedToBottom(atBottom);
    if (atBottom) setHasNew(false);
  }, []);

  const jumpToLive = useCallback(() => {
    const el = scrollerRef.current;
    if (!el) return;
    el.scrollTop = el.scrollHeight;
    setPinnedToBottom(true);
    setHasNew(false);
  }, []);

  const isLive = ws.status === "open";
  const liveDot = useMemo(
    () => (
      <span
        aria-label={isLive ? "live" : "offline"}
        title={isLive ? "live" : ws.status}
        style={{
          display: "inline-block",
          width: 8,
          height: 8,
          borderRadius: "50%",
          background: isLive ? "var(--accent)" : "var(--text-dim)",
          boxShadow: isLive ? "0 0 6px var(--accent)" : "none",
          marginRight: 6,
        }}
      />
    ),
    [isLive, ws.status],
  );

  return (
    <section className="card" data-testid="activity-log">
      <header
        style={{
          display: "flex",
          justifyContent: "space-between",
          alignItems: "center",
          marginBottom: "0.5rem",
        }}
      >
        <h2 style={{ margin: 0 }} className="accent">
          {">"} activity log
        </h2>
        <span
          className="dim"
          style={{
            fontSize: "0.8rem",
            display: "inline-flex",
            alignItems: "center",
            textTransform: "uppercase",
            letterSpacing: "0.06em",
          }}
        >
          {liveDot}
          {isLive ? "live" : ws.status}
        </span>
      </header>

      <div style={{ position: "relative" }}>
        <div
          ref={scrollerRef}
          onScroll={handleScroll}
          role="log"
          aria-live="polite"
          aria-relevant="additions"
          aria-label="Live activity log"
          data-testid="activity-log-scroller"
          style={{
            height: 240,
            overflowY: "auto",
            border: "1px solid var(--border)",
            background: "var(--bg-elev, rgba(0,0,0,0.25))",
            padding: "0.4rem 0.6rem",
            fontFamily: "var(--font-mono, 'JetBrains Mono', monospace)",
            fontSize: "0.82rem",
            lineHeight: 1.55,
          }}
        >
          {entries.length === 0 ? (
            <div
              className="dim"
              style={{
                height: "100%",
                display: "flex",
                alignItems: "center",
                justifyContent: "center",
                fontSize: "0.85rem",
                textAlign: "center",
              }}
            >
              waiting for activity… start the strategy to see live signals here.
            </div>
          ) : (
            entries.map((e) => <ActivityRow key={e.id} entry={e} />)
          )}
        </div>

        {hasNew && !pinnedToBottom && (
          <button
            onClick={jumpToLive}
            data-testid="activity-log-jump-pill"
            style={{
              position: "absolute",
              right: 12,
              bottom: 10,
              padding: "0.25rem 0.6rem",
              background: "var(--accent)",
              color: "var(--bg)",
              border: "none",
              cursor: "pointer",
              fontFamily: "inherit",
              fontWeight: 700,
              fontSize: "0.75rem",
              textTransform: "uppercase",
              letterSpacing: "0.06em",
              boxShadow: "0 0 8px var(--accent)",
            }}
          >
            ↓ new
          </button>
        )}
      </div>
    </section>
  );
}

/* -------------------------------------------------------------------------- */
/* Row                                                                        */
/* -------------------------------------------------------------------------- */

function ActivityRow({ entry }: { entry: ActivityEntry }) {
  const color = colorFor(entry.tone);
  return (
    <div
      data-testid="activity-log-row"
      data-kind={entry.kind}
      data-tone={entry.tone}
      style={{
        display: "grid",
        gridTemplateColumns: "auto auto auto 1fr auto",
        gap: "0.6rem",
        alignItems: "baseline",
        whiteSpace: "nowrap",
        overflow: "hidden",
        textOverflow: "ellipsis",
      }}
    >
      <span className="dim" style={{ opacity: 0.7 }}>
        {formatClock(entry.ts)}
      </span>
      <span style={{ color, width: "1ch", textAlign: "center" }} aria-hidden="true">
        {iconFor(entry.kind)}
      </span>
      <span
        style={{
          color,
          fontWeight: 700,
          textTransform: "uppercase",
          letterSpacing: "0.04em",
          minWidth: "5.5ch",
        }}
      >
        {labelFor(entry.kind)}
      </span>
      <span style={{ color: "var(--text)" }}>{entry.text}</span>
      {entry.detail && (
        <span className="dim" style={{ fontSize: "0.78rem" }}>
          {entry.detail}
        </span>
      )}
    </div>
  );
}
