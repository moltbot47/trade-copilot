"use client";

/**
 * ConnectionStatus — small terminal-themed pill rendered in Layout.
 *
 *   open          → green   ● LIVE
 *   reconnecting  → amber   ◑ RECONNECTING
 *   connecting    → amber   ◑ CONNECTING
 *   closed        → dim     ○ OFFLINE
 *
 * Uses CSS variables already defined in globals.css.
 */

import { useEffect, useState } from "react";
import { useWebSocket } from "@/hooks/useWebSocket";

function formatAgo(ms: number | null, now: number): string {
  if (ms === null) return "—";
  const diff = Math.max(0, Math.round((now - ms) / 1000));
  if (diff < 60) return `${diff}s ago`;
  if (diff < 3600) return `${Math.round(diff / 60)}m ago`;
  return `${Math.round(diff / 3600)}h ago`;
}

export default function ConnectionStatus() {
  const { status, subscribe } = useWebSocket();
  const [lastMessageAt, setLastMessageAt] = useState<number | null>(null);
  const [tick, setTick] = useState<number>(Date.now());

  // Track last activity by listening on every channel we currently care about.
  useEffect(() => {
    // We subscribe to "account" purely as a heartbeat indicator; pages that
    // need account data subscribe independently and the WS client de-dupes.
    const off = subscribe("account", () => {
      setLastMessageAt(Date.now());
    });
    return off;
  }, [subscribe]);

  useEffect(() => {
    const t = setInterval(() => setTick(Date.now()), 5000);
    return () => clearInterval(t);
  }, []);

  let glyph: string;
  let label: string;
  let color: string;

  switch (status) {
    case "open":
      glyph = "●";
      label = "LIVE";
      color = "var(--accent)";
      break;
    case "connecting":
      glyph = "◑";
      label = "CONNECTING";
      color = "var(--warn)";
      break;
    case "reconnecting":
      glyph = "◑";
      label = "RECONNECTING";
      color = "var(--warn)";
      break;
    case "closed":
    default:
      glyph = "○";
      label = "OFFLINE";
      color = "var(--text-dim)";
      break;
  }

  const tooltip =
    status === "open" && lastMessageAt
      ? `last message: ${formatAgo(lastMessageAt, tick)}`
      : `status: ${status}`;

  return (
    <span
      role="status"
      aria-live="polite"
      aria-label={`websocket ${label.toLowerCase()}`}
      title={tooltip}
      data-testid="connection-status"
      style={{
        display: "inline-flex",
        alignItems: "center",
        gap: "0.35rem",
        padding: "0.2rem 0.55rem",
        border: `1px solid ${color}`,
        color,
        fontSize: "0.72rem",
        fontWeight: 700,
        letterSpacing: "0.06em",
        textTransform: "uppercase",
        background: "transparent",
        whiteSpace: "nowrap",
      }}
    >
      <span aria-hidden="true">{glyph}</span>
      <span>{label}</span>
    </span>
  );
}
