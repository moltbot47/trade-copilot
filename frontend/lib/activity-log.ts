/**
 * Activity log helpers — Wave 5C
 *
 * Pure formatter/parser utilities for the live ActivityLog panel on /strategy.
 * No React, no DOM, no I/O — easy to unit test in isolation.
 */

import type {
  PositionsEvent,
  SignalsEvent,
  StrategyEvent,
  TradesEvent,
  WsChannel,
} from "./ws-types";
import type { TradeOutcome } from "./types";

/* -------------------------------------------------------------------------- */
/* Public types                                                               */
/* -------------------------------------------------------------------------- */

export type ActivityKind =
  | "signal"
  | "entry"
  | "scale"
  | "partial"
  | "sl_move"
  | "exit"
  | "error";

export type ActivityTone = "profit" | "loss" | "neutral" | "manage" | "error";

export interface ActivityEntry {
  /** Stable client-side ID — used as React key. Monotonically increasing. */
  id: string;
  /** Wall-clock timestamp the entry was created (ms since epoch). */
  ts: number;
  /** Channel that produced the event (for debugging / filtering). */
  channel: WsChannel | "internal";
  kind: ActivityKind;
  tone: ActivityTone;
  /** Single-line label — e.g. "BTCUSD buy  conf 1.84" */
  text: string;
  /** Optional secondary text (small, dim) */
  detail?: string;
}

/* -------------------------------------------------------------------------- */
/* Constants                                                                  */
/* -------------------------------------------------------------------------- */

export const MAX_ACTIVITY_ENTRIES = 50;

const KIND_ICON: Record<ActivityKind, string> = {
  signal: "▲",
  entry: "◇",
  scale: "◆",
  partial: "◇",
  sl_move: "▼",
  exit: "◇",
  error: "!",
};

const KIND_LABEL: Record<ActivityKind, string> = {
  signal: "SIGNAL",
  entry: "ENTRY",
  scale: "SCALE",
  partial: "PARTIAL",
  sl_move: "SL→",
  exit: "EXIT",
  error: "ERROR",
};

const TONE_COLOR: Record<ActivityTone, string> = {
  profit: "var(--accent)",
  loss: "var(--danger)",
  manage: "var(--warn)",
  neutral: "var(--text-dim)",
  error: "var(--danger)",
};

export function iconFor(kind: ActivityKind): string {
  return KIND_ICON[kind];
}

export function labelFor(kind: ActivityKind): string {
  return KIND_LABEL[kind];
}

export function colorFor(tone: ActivityTone): string {
  return TONE_COLOR[tone];
}

/* -------------------------------------------------------------------------- */
/* ID generation                                                              */
/* -------------------------------------------------------------------------- */

let _seq = 0;
export function nextActivityId(): string {
  _seq = (_seq + 1) >>> 0;
  return `a${Date.now().toString(36)}-${_seq.toString(36)}`;
}

/** Test-only: reset the monotonic id seed. */
export function __resetActivitySeq(): void {
  _seq = 0;
}

/* -------------------------------------------------------------------------- */
/* Formatting helpers                                                         */
/* -------------------------------------------------------------------------- */

/** HH:MM:SS in 24h, local tz. */
export function formatClock(ts: number): string {
  const d = new Date(ts);
  if (Number.isNaN(d.getTime())) return "--:--:--";
  const hh = String(d.getHours()).padStart(2, "0");
  const mm = String(d.getMinutes()).padStart(2, "0");
  const ss = String(d.getSeconds()).padStart(2, "0");
  return `${hh}:${mm}:${ss}`;
}

function fmtPrice(n: number | null | undefined): string {
  if (n === null || n === undefined || !Number.isFinite(n)) return "—";
  if (Math.abs(n) >= 1000) return n.toLocaleString(undefined, { maximumFractionDigits: 2 });
  return n.toFixed(n >= 100 ? 2 : 4);
}

function fmtUsd(n: number | null | undefined): string {
  if (n === null || n === undefined || !Number.isFinite(n)) return "—";
  const sign = n >= 0 ? "+" : "−";
  return `${sign}$${Math.abs(n).toFixed(2)}`;
}

function fmtConf(n: number | null | undefined): string {
  if (n === null || n === undefined || !Number.isFinite(n)) return "—";
  return n.toFixed(2);
}

function pnlTone(pnl: number | null | undefined): ActivityTone {
  if (pnl === null || pnl === undefined || !Number.isFinite(pnl)) return "neutral";
  return pnl >= 0 ? "profit" : "loss";
}

/* -------------------------------------------------------------------------- */
/* Channel-specific builders                                                  */
/* -------------------------------------------------------------------------- */

export function entryFromSignal(payload: SignalsEvent): ActivityEntry {
  const conf = fmtConf(payload.confidence);
  return {
    id: nextActivityId(),
    ts: Date.now(),
    channel: "signals",
    kind: "signal",
    tone: "profit",
    text: `${payload.symbol} ${payload.side}`,
    detail: `conf ${conf}`,
  };
}

export function entryFromPositions(payload: PositionsEvent): ActivityEntry | null {
  // Only "opened" frames produce an ENTRY row. "closed" → handled by trades
  // channel so we get the realised PnL. "updated" is too chatty for the log.
  if (payload.kind !== "opened") return null;
  return {
    id: nextActivityId(),
    ts: Date.now(),
    channel: "positions",
    kind: "entry",
    tone: "profit",
    text: `${payload.symbol} ${payload.side} ${payload.qty}`,
    detail: `@ ${fmtPrice(payload.avg_price)}`,
  };
}

export function entryFromTrade(payload: TradesEvent): ActivityEntry {
  const tone = pnlTone(payload.pnl_usd);
  return {
    id: nextActivityId(),
    ts: Date.now(),
    channel: "trades",
    kind: "exit",
    tone,
    text: `${payload.instrument} final → ${fmtUsd(payload.pnl_usd)}`,
    detail: `${payload.side} · R ${payload.r_multiple.toFixed(2)}`,
  };
}

/**
 * Diff a strategy snapshot against the previous one to detect:
 *   - SCALE  (a position grew in qty without going to 0 first — pyramid add)
 *   - PARTIAL (a position shrank but didn't close — partial close)
 *   - SL→BE   (snapshot exposes a `breakeven` flag in trade meta — best-effort)
 *
 * StrategyEvent doesn't carry per-position deltas natively, so we infer from
 * `recent_trades` open snapshots. This is intentionally conservative: if we
 * can't tell, we emit nothing.
 */
export function diffSnapshotForActivity(
  prev: StrategyEvent | null,
  next: StrategyEvent,
): ActivityEntry[] {
  if (!prev) return [];
  const entries: ActivityEntry[] = [];

  const prevByKey = new Map<string, TradeOutcome>();
  for (const t of prev.recent_trades || []) {
    prevByKey.set(`${t.id}`, t);
  }

  for (const t of next.recent_trades || []) {
    const before = prevByKey.get(`${t.id}`);
    if (!before) continue;
    // Pyramid add — qty grew, position still open (no exit_price change).
    if (t.qty > before.qty + 1e-9 && t.exit_price === before.exit_price) {
      entries.push({
        id: nextActivityId(),
        ts: Date.now(),
        channel: "internal",
        kind: "scale",
        tone: "manage",
        text: `${t.instrument} +${(t.qty - before.qty).toFixed(2)}`,
        detail: `@ ${fmtPrice(t.entry_price)}`,
      });
    } else if (t.qty < before.qty - 1e-9 && t.qty > 1e-9) {
      // Partial close — qty shrank but still > 0.
      const pct = before.qty > 0 ? Math.round(((before.qty - t.qty) / before.qty) * 100) : 0;
      entries.push({
        id: nextActivityId(),
        ts: Date.now(),
        channel: "internal",
        kind: "partial",
        tone: "manage",
        text: `${t.instrument} -${pct}%`,
        detail: `@ ${fmtPrice(t.exit_price)}`,
      });
    }
  }

  return entries;
}

export function entryFromError(message: string): ActivityEntry {
  return {
    id: nextActivityId(),
    ts: Date.now(),
    channel: "internal",
    kind: "error",
    tone: "error",
    text: message,
  };
}

/* -------------------------------------------------------------------------- */
/* Ring buffer                                                                */
/* -------------------------------------------------------------------------- */

/** Append entries to the ring buffer, trimming oldest entries past the cap. */
export function appendToRing(
  buffer: ActivityEntry[],
  newEntries: ActivityEntry[],
  cap: number = MAX_ACTIVITY_ENTRIES,
): ActivityEntry[] {
  if (newEntries.length === 0) return buffer;
  const next = buffer.concat(newEntries);
  if (next.length <= cap) return next;
  return next.slice(next.length - cap);
}
