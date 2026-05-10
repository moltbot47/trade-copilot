"use client";

import { useEffect, useMemo, useState } from "react";
import Link from "next/link";
import { api } from "@/lib/api";
import EmailGate from "@/components/EmailGate";
import { formatLocalTime } from "@/lib/datetime";

type AuditRow = {
  ts: string;
  action: string;
  details: string;
  client_ip: string | null;
};

// User-friendly labels for the action enum strings.
const ACTION_LABELS: Record<string, { label: string; color: string }> = {
  login_success: { label: "logged in", color: "var(--accent)" },
  login_failed: { label: "login failed", color: "var(--warn)" },
  logout: { label: "logged out", color: "var(--text-dim)" },
  account_locked: { label: "account locked", color: "var(--danger)" },
  account_unlocked: { label: "account unlocked", color: "var(--accent)" },
  mfa_setup_initiated: { label: "MFA setup started", color: "var(--text)" },
  mfa_enabled: { label: "MFA enabled", color: "var(--accent)" },
  mfa_disabled: { label: "MFA disabled", color: "var(--warn)" },
  mfa_invalid_code: { label: "MFA code rejected", color: "var(--danger)" },
  tradelocker_connected: { label: "broker connected", color: "var(--accent)" },
  tradelocker_disconnected: { label: "broker disconnected", color: "var(--text-dim)" },
  bot_paused: { label: "PANIC: bot paused", color: "var(--danger)" },
  bot_unpaused: { label: "bot resumed", color: "var(--accent)" },
  risk_setting_changed: { label: "settings changed", color: "var(--text)" },
  strategy_started: { label: "strategy started", color: "var(--accent)" },
  strategy_stopped: { label: "strategy stopped", color: "var(--text-dim)" },
  order_placed: { label: "order placed", color: "var(--text)" },
};

function actionLabel(action: string) {
  return ACTION_LABELS[action] || { label: action, color: "var(--text-dim)" };
}

export default function AuditLogPage() {
  const [rows, setRows] = useState<AuditRow[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [filter, setFilter] = useState<string>("");

  const load = async () => {
    setLoading(true);
    setError(null);
    try {
      const r = await api.getAuditLog(100);
      setRows(r);
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    load();
  }, []);

  const filtered = useMemo(() => {
    if (!filter) return rows;
    return rows.filter((r) =>
      r.action.toLowerCase().includes(filter.toLowerCase())
    );
  }, [rows, filter]);

  // Distinct actions for the filter dropdown
  const actionTypes = useMemo(() => {
    const s = new Set(rows.map((r) => r.action));
    return Array.from(s).sort();
  }, [rows]);

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: "1.25rem" }}>
      <EmailGate />
      <header>
        <div className="dim" style={{ fontSize: "0.85rem" }}>
          <Link href="/settings" style={{ color: "var(--text-dim)" }}>
            ‹ settings
          </Link>
          {" > "}audit log
        </div>
        <h1 style={{ margin: "0.25rem 0" }}>
          <span className="accent">Audit log</span>
        </h1>
        <p
          className="dim"
          style={{ margin: 0, fontSize: "0.88rem", maxWidth: "60ch" }}
        >
          Every security-sensitive action taken on your account. If you see
          something you didn't do, change your password immediately and
          enable MFA. Only YOUR actions are shown — never anyone else's.
        </p>
      </header>

      {/* Filter bar */}
      <section
        className="card"
        style={{
          padding: "0.75rem 1rem",
          display: "flex",
          alignItems: "center",
          gap: "0.75rem",
          flexWrap: "wrap",
        }}
      >
        <span className="dim" style={{ fontSize: "0.85rem" }}>
          filter:
        </span>
        <select
          value={filter}
          onChange={(e) => setFilter(e.target.value)}
          style={{
            padding: "0.4rem 0.6rem",
            background: "var(--bg)",
            color: "var(--text)",
            border: "1px solid var(--border)",
            fontFamily: "inherit",
          }}
        >
          <option value="">all actions</option>
          {actionTypes.map((a) => (
            <option key={a} value={a}>
              {actionLabel(a).label}
            </option>
          ))}
        </select>
        <button
          onClick={load}
          className="btn"
          style={{ padding: "0.4rem 0.85rem", fontSize: "0.85rem" }}
        >
          refresh
        </button>
        <span className="dim" style={{ fontSize: "0.78rem", marginLeft: "auto" }}>
          showing {filtered.length} of {rows.length} entries
        </span>
      </section>

      {/* Table */}
      <section className="card" style={{ padding: 0, overflow: "hidden" }}>
        {loading && (
          <p className="dim" style={{ padding: "1rem", margin: 0 }}>
            loading…
          </p>
        )}
        {error && !loading && (
          <p
            role="alert"
            className="danger"
            style={{ padding: "1rem", margin: 0 }}
          >
            error: {error}
          </p>
        )}
        {!loading && !error && filtered.length === 0 && (
          <p className="dim" style={{ padding: "1rem", margin: 0 }}>
            {rows.length === 0
              ? "no entries yet — they will appear here after you log in, change settings, or take any security action"
              : "no entries match the filter"}
          </p>
        )}
        {!loading && filtered.length > 0 && (
          <table
            style={{
              width: "100%",
              borderCollapse: "collapse",
              fontSize: "0.88rem",
            }}
          >
            <thead>
              <tr
                style={{
                  borderBottom: "1px solid var(--border)",
                  textAlign: "left",
                }}
              >
                <th style={{ padding: "0.6rem 0.85rem" }}>time</th>
                <th style={{ padding: "0.6rem 0.85rem" }}>action</th>
                <th style={{ padding: "0.6rem 0.85rem" }}>details</th>
                <th style={{ padding: "0.6rem 0.85rem" }}>ip</th>
              </tr>
            </thead>
            <tbody>
              {filtered.map((r, i) => {
                const lbl = actionLabel(r.action);
                let parsed: Record<string, unknown> = {};
                try {
                  parsed = JSON.parse(r.details);
                } catch {
                  /* leave empty */
                }
                const detailStr =
                  Object.keys(parsed).length === 0
                    ? "—"
                    : Object.entries(parsed)
                        .slice(0, 4)
                        .map(([k, v]) => `${k}=${String(v).slice(0, 30)}`)
                        .join(" ");

                return (
                  <tr
                    key={i}
                    style={{ borderBottom: "1px solid var(--border)" }}
                  >
                    <td
                      style={{
                        padding: "0.55rem 0.85rem",
                        fontFamily: "JetBrains Mono, monospace",
                        fontSize: "0.78rem",
                        whiteSpace: "nowrap",
                      }}
                    >
                      {formatLocalTime(r.ts)}
                    </td>
                    <td
                      style={{
                        padding: "0.55rem 0.85rem",
                        color: lbl.color,
                        fontWeight: 600,
                      }}
                    >
                      {lbl.label}
                    </td>
                    <td
                      className="dim"
                      style={{
                        padding: "0.55rem 0.85rem",
                        fontFamily: "JetBrains Mono, monospace",
                        fontSize: "0.78rem",
                        maxWidth: "40ch",
                        overflow: "hidden",
                        textOverflow: "ellipsis",
                        whiteSpace: "nowrap",
                      }}
                    >
                      {detailStr}
                    </td>
                    <td
                      className="dim"
                      style={{
                        padding: "0.55rem 0.85rem",
                        fontFamily: "JetBrains Mono, monospace",
                        fontSize: "0.78rem",
                      }}
                    >
                      {r.client_ip || "—"}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        )}
      </section>
    </div>
  );
}
