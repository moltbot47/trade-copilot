"use client";

import { useEffect, useState } from "react";
import { api } from "@/lib/api";
import EmailGate from "@/components/EmailGate";
import RiskSlider from "@/components/RiskSlider";
import PnLChart from "@/components/PnLChart";
import SignalLog from "@/components/SignalLog";
import type {
  AccountState,
  Subscription,
  PnLPoint,
  Signal,
  Bot,
} from "@/lib/types";

export default function DashboardPage() {
  const [account, setAccount] = useState<AccountState | null>(null);
  const [accountErr, setAccountErr] = useState<string | null>(null);

  const [subs, setSubs] = useState<Subscription[]>([]);
  const [bots, setBots] = useState<Record<number, Bot>>({});
  const [subsErr, setSubsErr] = useState<string | null>(null);

  const [pnl, setPnl] = useState<PnLPoint[]>([]);
  const [signals, setSignals] = useState<Signal[]>([]);
  const [logsErr, setLogsErr] = useState<string | null>(null);

  const [loading, setLoading] = useState(true);

  const loadAll = async () => {
    setLoading(true);
    const tasks = await Promise.allSettled([
      api.getAccountState(),
      api.getSubscriptions(),
      api.getBots(),
      api.getDashboardPnL(),
      api.getPositions(),
    ]);

    const [accRes, subRes, botRes, pnlRes, posRes] = tasks;

    if (accRes.status === "fulfilled") {
      setAccount(accRes.value);
      setAccountErr(null);
    } else {
      setAccountErr(accRes.reason?.message || "error");
    }

    if (subRes.status === "fulfilled") {
      setSubs(subRes.value);
      setSubsErr(null);
    } else {
      setSubsErr(subRes.reason?.message || "error");
    }

    if (botRes.status === "fulfilled") {
      const map: Record<number, Bot> = {};
      botRes.value.forEach((b) => (map[b.id] = b));
      setBots(map);
    }

    if (pnlRes.status === "fulfilled") setPnl(pnlRes.value);
    if (posRes.status === "fulfilled") setSignals(posRes.value);
    if (pnlRes.status === "rejected" && posRes.status === "rejected") {
      setLogsErr(pnlRes.reason?.message || "error");
    } else {
      setLogsErr(null);
    }

    setLoading(false);
  };

  useEffect(() => {
    loadAll();
    const t = setInterval(loadAll, 15000);
    return () => clearInterval(t);
  }, []);

  const updateAggression = async (subId: number, aggression: number) => {
    setSubs((curr) =>
      curr.map((s) => (s.id === subId ? { ...s, aggression } : s))
    );
    try {
      await api.updateSubscription(subId, { aggression });
    } catch (err) {
      setSubsErr((err as Error).message);
    }
  };

  const togglePause = async (sub: Subscription) => {
    const next = !sub.paused;
    setSubs((curr) =>
      curr.map((s) => (s.id === sub.id ? { ...s, paused: next } : s))
    );
    try {
      await api.updateSubscription(sub.id, { paused: next });
    } catch (err) {
      setSubsErr((err as Error).message);
      // revert
      setSubs((curr) =>
        curr.map((s) => (s.id === sub.id ? { ...s, paused: !next } : s))
      );
    }
  };

  const backendOffline =
    accountErr === "Backend offline" &&
    subsErr === "Backend offline" &&
    logsErr === "Backend offline";

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: "1.25rem" }}>
      <EmailGate />

      <header>
        <div className="dim" style={{ fontSize: "0.85rem" }}>
          {">"} live console
        </div>
        <h1 style={{ marginTop: "0.25rem" }}>dashboard</h1>
        <p className="dim">auto-refresh: 15s</p>
      </header>

      {backendOffline && (
        <div className="card" style={{ borderColor: "var(--danger)" }}>
          <strong className="danger">backend offline.</strong>{" "}
          <span className="dim">
            Start the API at{" "}
            {process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000"}.
          </span>
        </div>
      )}

      {/* Account state */}
      <section className="card">
        <h2 style={{ marginTop: 0 }} className="accent">
          {">"} account
        </h2>
        {loading && !account && <p className="dim">loading...</p>}
        {accountErr && !account && (
          <p className="dim" style={{ fontSize: "0.85rem" }}>
            {accountErr === "Backend offline"
              ? "backend offline"
              : "no session — connect TradeLocker"}
          </p>
        )}
        {account && (
          <div
            style={{
              display: "grid",
              gridTemplateColumns: "repeat(auto-fit, minmax(140px, 1fr))",
              gap: "1rem",
            }}
          >
            <Stat label="balance" value={`$${account.balance?.toFixed(2) ?? "—"}`} />
            <Stat label="equity" value={`$${account.equity?.toFixed(2) ?? "—"}`} />
            <Stat
              label="open pnl"
              value={`$${account.open_pnl?.toFixed(2) ?? "—"}`}
              color={
                (account.open_pnl ?? 0) >= 0
                  ? "var(--accent)"
                  : "var(--danger)"
              }
            />
            <Stat
              label="account"
              value={account.account_id || "—"}
              small
            />
          </div>
        )}
      </section>

      {/* Subscriptions */}
      <section className="card">
        <h2 style={{ marginTop: 0 }} className="accent">
          {">"} active subscriptions
        </h2>
        {loading && subs.length === 0 && !subsErr && (
          <p className="dim">loading...</p>
        )}
        {subs.length === 0 && !loading && (
          <p className="dim" style={{ fontSize: "0.85rem" }}>
            no active subscriptions — visit /bots to subscribe
          </p>
        )}
        <div
          style={{
            display: "grid",
            gridTemplateColumns: "repeat(auto-fit, minmax(280px, 1fr))",
            gap: "1rem",
          }}
        >
          {subs.map((s) => {
            const bot = bots[s.bot_id];
            return (
              <div
                key={s.id}
                className="card"
                style={{ background: "var(--bg)" }}
              >
                <div
                  style={{
                    display: "flex",
                    justifyContent: "space-between",
                    alignItems: "center",
                    marginBottom: "0.5rem",
                  }}
                >
                  <h3 style={{ margin: 0 }} className="accent">
                    {bot?.name || s.bot_name || `bot #${s.bot_id}`}
                  </h3>
                  <button
                    onClick={() => togglePause(s)}
                    className="btn"
                    style={{
                      padding: "0.25rem 0.6rem",
                      fontSize: "0.75rem",
                      borderColor: s.paused ? "var(--warn)" : "var(--accent-dim)",
                      color: s.paused ? "var(--warn)" : "var(--accent)",
                    }}
                  >
                    {s.paused ? "paused" : "running"}
                  </button>
                </div>
                <RiskSlider
                  value={s.aggression}
                  onChange={(v) => updateAggression(s.id, v)}
                  disabled={s.paused}
                />
              </div>
            );
          })}
        </div>
      </section>

      {/* PnL + Signals */}
      <section className="card">
        <h2 style={{ marginTop: 0 }} className="accent">
          {">"} pnl
        </h2>
        <PnLChart data={pnl} />
      </section>

      <section className="card">
        <h2 style={{ marginTop: 0 }} className="accent">
          {">"} recent signals
        </h2>
        <SignalLog signals={signals} />
      </section>
    </div>
  );
}

function Stat({
  label,
  value,
  color,
  small,
}: {
  label: string;
  value: string;
  color?: string;
  small?: boolean;
}) {
  return (
    <div>
      <div className="dim" style={{ fontSize: "0.78rem" }}>
        {label}
      </div>
      <div
        style={{
          color: color || "var(--text)",
          fontSize: small ? "0.95rem" : "1.4rem",
          fontWeight: 600,
        }}
      >
        {value}
      </div>
    </div>
  );
}
