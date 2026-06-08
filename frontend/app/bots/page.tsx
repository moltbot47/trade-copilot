"use client";

import { useEffect, useState } from "react";
import { api } from "@/lib/api";
import type { Bot } from "@/lib/types";
import BotCard from "@/components/BotCard";

export default function BotsPage() {
  const [bots, setBots] = useState<Bot[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  // undefined = unknown (still loading or logged out); true/false = resolved.
  const [brokerConnected, setBrokerConnected] = useState<boolean | undefined>(
    undefined,
  );

  useEffect(() => {
    let mounted = true;
    api
      .getBots()
      .then((b) => {
        if (mounted) setBots(b);
      })
      .catch((e: Error) => {
        if (mounted) setError(e.message);
      })
      .finally(() => {
        if (mounted) setLoading(false);
      });
    api
      .me()
      .then((me) => {
        if (mounted) setBrokerConnected(Boolean(me.tradelocker_account_id));
      })
      .catch(() => {
        // 401 (not logged in) or network error — treat as not connected so
        // the "connect broker first" link still shows. The Subscribe button's
        // own click handler will route logged-out users to /strategy where
        // the EmailGate prompts them.
        if (mounted) setBrokerConnected(false);
      });
    return () => {
      mounted = false;
    };
  }, []);

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: "1.25rem" }}>
      <header>
        <div className="dim" style={{ fontSize: "0.85rem" }}>
          {">"} marketplace
        </div>
        <h1 style={{ marginTop: "0.25rem" }}>strategy bots</h1>
        <p className="dim">
          Pick a bot. Backtested numbers below are historical, not promises.
        </p>
      </header>

      {loading && <p className="dim">loading bots...</p>}
      {error && (
        <div className="card" style={{ borderColor: "var(--danger)" }}>
          <strong className="danger">backend offline:</strong> {error}
          <p className="dim" style={{ marginBottom: 0, fontSize: "0.85rem" }}>
            Make sure the API is running at{" "}
            {process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000"}.
          </p>
        </div>
      )}
      {!loading && !error && bots && bots.length === 0 && (
        <p className="dim">no bots configured yet</p>
      )}
      {bots && bots.length > 0 && (
        <div
          style={{
            display: "grid",
            gridTemplateColumns: "repeat(auto-fit, minmax(280px, 1fr))",
            gap: "1rem",
          }}
        >
          {bots.map((b) => (
            <BotCard key={b.id} bot={b} brokerConnected={brokerConnected} />
          ))}
        </div>
      )}
    </div>
  );
}
