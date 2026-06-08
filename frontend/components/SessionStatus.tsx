"use client";

/**
 * SessionStatus — TradeLocker session-health pill rendered in Layout.
 *
 *   ok            → green   ● TL OK
 *   expired       → red     ✗ TL EXPIRED  (clickable → /connect)
 *   network_error → amber   ◑ TL UNREACHABLE
 *   disconnected  → dim     ○ TL OFFLINE  (clickable → /connect)
 *   loading       → dim     ◌ TL …
 *
 * Pairs with the /api/tradelocker/session-status backend probe. UI surfaces
 * the same state the strategy/start endpoint uses to gate launches, so the
 * user knows *before* they click Start whether they need to reconnect.
 *
 * Style mirrors ConnectionStatus.tsx — terminal/TUI pill, currentColor
 * via CSS vars, no Tailwind dependency.
 */

import Link from "next/link";
import { useEffect, useRef, useState } from "react";
import { api, getUserEmail } from "@/lib/api";

type SessionState =
  | { kind: "loading" }
  | { kind: "ok" }
  | { kind: "expired" }
  | { kind: "network_error" }
  | { kind: "disconnected" }
  | { kind: "hidden" }; // user not signed in

const POLL_INTERVAL_MS = 60_000;

export default function SessionStatus() {
  const [state, setState] = useState<SessionState>({ kind: "loading" });
  const cancelledRef = useRef(false);

  useEffect(() => {
    cancelledRef.current = false;
    const email = getUserEmail();
    if (!email) {
      setState({ kind: "hidden" });
      return;
    }

    const probe = async () => {
      try {
        const r = await api.getTradeLockerSessionStatus();
        if (cancelledRef.current) return;
        // Map backend status strings → UI state.
        if (!r.connected || r.status === "disconnected" || r.status === "no_session") {
          setState({ kind: "disconnected" });
        } else if (r.valid && r.status === "ok") {
          setState({ kind: "ok" });
        } else if (r.status === "needs_reauth" || r.status === "expired") {
          setState({ kind: "expired" });
        } else if (r.status === "network_error") {
          setState({ kind: "network_error" });
        } else {
          // Defensive: unknown status → treat as expired so user gets prompted
          setState({ kind: "expired" });
        }
      } catch {
        // Auth missing OR transport failure — fall back to network_error.
        // The /connect link still works, so the user has a way out.
        if (!cancelledRef.current) setState({ kind: "network_error" });
      }
    };

    void probe();
    const t = setInterval(() => void probe(), POLL_INTERVAL_MS);
    return () => {
      cancelledRef.current = true;
      clearInterval(t);
    };
  }, []);

  if (state.kind === "hidden") return null;

  let glyph: string;
  let label: string;
  let color: string;
  let title: string;
  let href: string | null = null;

  switch (state.kind) {
    case "ok":
      glyph = "●";
      label = "TL OK";
      color = "var(--accent)";
      title = "TradeLocker session healthy";
      break;
    case "expired":
      glyph = "✗";
      label = "TL EXPIRED";
      color = "var(--danger, #ff5c5c)";
      title = "TradeLocker session expired — click to reconnect";
      href = "/connect";
      break;
    case "network_error":
      glyph = "◑";
      label = "TL UNREACHABLE";
      color = "var(--warn)";
      title = "Could not reach TradeLocker — retrying";
      break;
    case "disconnected":
      glyph = "○";
      label = "TL OFFLINE";
      color = "var(--text-dim)";
      title = "No TradeLocker session — click to connect";
      href = "/connect";
      break;
    case "loading":
    default:
      glyph = "◌";
      label = "TL …";
      color = "var(--text-dim)";
      title = "Checking TradeLocker session";
      break;
  }

  const pillStyle: React.CSSProperties = {
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
    textDecoration: "none",
    cursor: href ? "pointer" : "default",
  };

  const content = (
    <>
      <span aria-hidden="true">{glyph}</span>
      <span>{label}</span>
    </>
  );

  if (href) {
    return (
      <Link
        href={href}
        title={title}
        data-testid="session-status"
        role="status"
        aria-live="polite"
        aria-label={`tradelocker ${label.toLowerCase()}`}
        style={pillStyle}
      >
        {content}
      </Link>
    );
  }

  return (
    <span
      role="status"
      aria-live="polite"
      aria-label={`tradelocker ${label.toLowerCase()}`}
      title={title}
      data-testid="session-status"
      style={pillStyle}
    >
      {content}
    </span>
  );
}
