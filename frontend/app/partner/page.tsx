"use client";

/**
 * Partner dashboard — read-only view scoped to the calling user's
 * accessible TradingAccounts. Shows:
 *
 *   - Account picker (owned + granted accounts)
 *   - Today's summary card (signals, P&L pair, edge erosion, latency p95)
 *   - Slippage records table (filterable by strategy + status)
 *   - Click-through to record detail with raw broker JSON
 *   - Broker statements list + per-statement discrepancies panel
 *
 * Polls the daily summary + slippage records every 30s during active
 * sessions for near-real-time updates without a WS subscription. Click
 * "Refresh" for an immediate pull.
 *
 * Auth: standard session cookie / Authorization header (handled by api.ts).
 * If no user is signed in, the EmailGate banner is shown.
 */

import { useEffect, useMemo, useState } from "react";
import {
  api,
  getUserEmail,
  type PartnerSlippageRecord,
  type PartnerSlippageRecordFull,
  type PartnerDailySummary,
} from "@/lib/api";
import EmailGate from "@/components/EmailGate";

const POLL_INTERVAL_MS = 30_000;

type AccountOption = {
  id: number;
  label: string;
  tradelocker_account_id: string;
  env: string;
  role: string;
  owner_email: string | null;
  expires_at?: string | null;
};

export default function PartnerDashboardPage() {
  const [email, setEmail] = useState<string | null>(null);
  const [accounts, setAccounts] = useState<AccountOption[]>([]);
  const [selectedAccountId, setSelectedAccountId] = useState<number | null>(null);
  const [summary, setSummary] = useState<PartnerDailySummary | null>(null);
  const [records, setRecords] = useState<PartnerSlippageRecord[]>([]);
  const [statusFilter, setStatusFilter] = useState<string>("");
  const [strategyFilter, setStrategyFilter] = useState<string>("");
  const [activeRecord, setActiveRecord] = useState<PartnerSlippageRecordFull | null>(
    null,
  );
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    setEmail(getUserEmail());
  }, []);

  // Initial account list — also picks the first account as the default.
  useEffect(() => {
    if (!email) return;
    let cancelled = false;
    (async () => {
      try {
        const res = await api.partnerAccounts();
        if (cancelled) return;
        setAccounts(res.accounts);
        if (res.accounts.length > 0 && selectedAccountId === null) {
          setSelectedAccountId(res.accounts[0].id);
        }
      } catch (err) {
        if (!cancelled) setError((err as Error).message);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [email]); // eslint-disable-line react-hooks/exhaustive-deps

  const loadAccountData = async (accountId: number) => {
    setLoading(true);
    setError(null);
    try {
      const [s, r] = await Promise.all([
        api.partnerDailySummary(accountId, undefined, strategyFilter || undefined),
        api.partnerSlippageRecords({
          accountId,
          strategyName: strategyFilter || undefined,
          status: statusFilter || undefined,
          limit: 200,
        }),
      ]);
      setSummary(s);
      setRecords(r.records);
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setLoading(false);
    }
  };

  // Polling effect — re-fires on account/filter changes and every 30s.
  useEffect(() => {
    if (selectedAccountId === null) return;
    void loadAccountData(selectedAccountId);
    const t = setInterval(
      () => void loadAccountData(selectedAccountId),
      POLL_INTERVAL_MS,
    );
    return () => clearInterval(t);
  }, [selectedAccountId, statusFilter, strategyFilter]); // eslint-disable-line react-hooks/exhaustive-deps

  const selectedAccount = useMemo(
    () => accounts.find((a) => a.id === selectedAccountId) ?? null,
    [accounts, selectedAccountId],
  );

  if (!email) {
    return (
      <main style={{ padding: "1.5rem" }}>
        <h1 style={{ marginTop: 0 }}>partner audit</h1>
        <EmailGate />
      </main>
    );
  }

  return (
    <main style={{ padding: "1.5rem", maxWidth: 1280, margin: "0 auto" }}>
      <header style={{ marginBottom: "1.5rem" }}>
        <h1 style={{ marginTop: 0, marginBottom: "0.25rem" }}>partner audit</h1>
        <p className="dim" style={{ fontSize: "0.85rem", marginTop: 0 }}>
          Read-only view of every account you can access. P&amp;L and slippage
          numbers refresh every 30s. Click a trade for the raw broker JSON.
        </p>
      </header>

      {error && (
        <div
          role="alert"
          style={{
            border: "1px solid var(--danger, #ff5c5c)",
            color: "var(--danger, #ff5c5c)",
            padding: "0.6rem 0.85rem",
            marginBottom: "1rem",
            fontSize: "0.8rem",
          }}
        >
          {error}
        </div>
      )}

      <AccountPicker
        accounts={accounts}
        selectedId={selectedAccountId}
        onSelect={setSelectedAccountId}
      />

      {selectedAccount && (
        <p className="dim" style={{ fontSize: "0.78rem", margin: "0.5rem 0 1.5rem" }}>
          {selectedAccount.role} access on{" "}
          <strong>{selectedAccount.tradelocker_account_id}</strong>
          {selectedAccount.owner_email ? ` (owner: ${selectedAccount.owner_email})` : ""}
          {selectedAccount.expires_at
            ? ` · expires ${new Date(selectedAccount.expires_at).toLocaleDateString()}`
            : ""}
        </p>
      )}

      <SummaryCard summary={summary} loading={loading} />

      <Filters
        strategyFilter={strategyFilter}
        statusFilter={statusFilter}
        onStrategyChange={setStrategyFilter}
        onStatusChange={setStatusFilter}
      />

      <RecordsTable
        records={records}
        loading={loading}
        onSelect={async (id) => {
          try {
            const full = await api.partnerSlippageRecord(id);
            setActiveRecord(full);
          } catch (err) {
            setError((err as Error).message);
          }
        }}
      />

      {activeRecord && (
        <RecordDetailModal
          record={activeRecord}
          onClose={() => setActiveRecord(null)}
        />
      )}
    </main>
  );
}

/* --------------------------------------------------------------------- */
/* sub-components                                                        */
/* --------------------------------------------------------------------- */

function AccountPicker({
  accounts,
  selectedId,
  onSelect,
}: {
  accounts: AccountOption[];
  selectedId: number | null;
  onSelect: (id: number) => void;
}) {
  if (accounts.length === 0) {
    return (
      <div className="dim" style={{ fontSize: "0.85rem" }}>
        No accessible accounts yet. The owner must grant you viewer or trader
        access to a TradingAccount before data appears here.
      </div>
    );
  }
  return (
    <select
      value={selectedId ?? ""}
      onChange={(e) => onSelect(Number(e.target.value))}
      style={{
        padding: "0.45rem 0.6rem",
        background: "var(--bg-elev, #111)",
        color: "currentColor",
        border: "1px solid var(--border)",
        fontSize: "0.85rem",
      }}
    >
      {accounts.map((a) => (
        <option key={a.id} value={a.id}>
          [{a.role}] {a.label} ({a.tradelocker_account_id})
        </option>
      ))}
    </select>
  );
}

function SummaryCard({
  summary,
  loading,
}: {
  summary: PartnerDailySummary | null;
  loading: boolean;
}) {
  if (!summary) {
    return (
      <section
        style={{
          border: "1px solid var(--border)",
          padding: "1rem",
          marginBottom: "1.5rem",
          fontSize: "0.85rem",
        }}
      >
        {loading ? "loading summary…" : "no summary yet"}
      </section>
    );
  }
  const erosionColor =
    summary.edge_erosion_pts > 0 ? "var(--warn)" : "var(--accent)";
  return (
    <section
      style={{
        border: "1px solid var(--border)",
        padding: "1rem 1.25rem",
        marginBottom: "1.5rem",
        display: "grid",
        gridTemplateColumns: "repeat(auto-fit, minmax(150px, 1fr))",
        gap: "0.6rem 1.2rem",
        fontSize: "0.85rem",
      }}
    >
      <Cell label="day (UTC)" value={summary.day} />
      <Cell
        label="trades closed"
        value={`${summary.trades_closed} / ${summary.signals_emitted}`}
      />
      <Cell
        label="strategy P&L (pts)"
        value={summary.strategy_pnl_pts.toFixed(2)}
      />
      <Cell label="real P&L (pts)" value={summary.real_pnl_pts.toFixed(2)} />
      <Cell
        label="edge erosion"
        value={`${summary.edge_erosion_pts.toFixed(2)} pts · $${summary.edge_erosion_dollars.toFixed(2)}`}
        color={erosionColor}
      />
      <Cell
        label="entry slippage (avg / worst)"
        value={`${summary.avg_entry_slippage_pts.toFixed(2)} / ${summary.worst_entry_slippage_pts.toFixed(2)} pts`}
      />
      <Cell
        label="latency (p95 / worst)"
        value={`${summary.p95_total_latency_ms} / ${summary.worst_total_latency_ms} ms`}
      />
      <Cell label="signals rejected" value={String(summary.signals_rejected)} />
    </section>
  );
}

function Cell({
  label,
  value,
  color,
}: {
  label: string;
  value: string;
  color?: string;
}) {
  return (
    <div>
      <div className="dim" style={{ fontSize: "0.72rem", textTransform: "uppercase" }}>
        {label}
      </div>
      <div style={{ fontWeight: 600, color }}>{value}</div>
    </div>
  );
}

function Filters({
  strategyFilter,
  statusFilter,
  onStrategyChange,
  onStatusChange,
}: {
  strategyFilter: string;
  statusFilter: string;
  onStrategyChange: (v: string) => void;
  onStatusChange: (v: string) => void;
}) {
  const inputStyle: React.CSSProperties = {
    padding: "0.35rem 0.55rem",
    background: "var(--bg-elev, #111)",
    color: "currentColor",
    border: "1px solid var(--border)",
    fontSize: "0.78rem",
    fontFamily: "inherit",
  };
  return (
    <div
      style={{
        display: "flex",
        gap: "0.6rem",
        marginBottom: "0.75rem",
        flexWrap: "wrap",
      }}
    >
      <input
        type="text"
        placeholder="strategy filter…"
        value={strategyFilter}
        onChange={(e) => onStrategyChange(e.target.value)}
        style={inputStyle}
      />
      <select
        value={statusFilter}
        onChange={(e) => onStatusChange(e.target.value)}
        style={inputStyle}
      >
        <option value="">all statuses</option>
        <option value="pending">pending</option>
        <option value="open">open</option>
        <option value="closed">closed</option>
        <option value="rejected">rejected</option>
      </select>
    </div>
  );
}

function RecordsTable({
  records,
  loading,
  onSelect,
}: {
  records: PartnerSlippageRecord[];
  loading: boolean;
  onSelect: (id: number) => void;
}) {
  if (loading && records.length === 0) {
    return <div className="dim" style={{ fontSize: "0.85rem" }}>loading…</div>;
  }
  if (records.length === 0) {
    return (
      <div className="dim" style={{ fontSize: "0.85rem" }}>
        no slippage records in the selected window.
      </div>
    );
  }
  const cellStyle: React.CSSProperties = {
    padding: "0.4rem 0.6rem",
    borderBottom: "1px solid var(--border)",
    textAlign: "left",
    whiteSpace: "nowrap",
    fontSize: "0.78rem",
  };
  return (
    <div style={{ overflowX: "auto", border: "1px solid var(--border)" }}>
      <table style={{ width: "100%", borderCollapse: "collapse" }}>
        <thead>
          <tr style={{ background: "var(--bg-elev, #111)" }}>
            <th style={cellStyle}>id</th>
            <th style={cellStyle}>status</th>
            <th style={cellStyle}>strategy</th>
            <th style={cellStyle}>symbol</th>
            <th style={cellStyle}>side</th>
            <th style={cellStyle}>entry slip</th>
            <th style={cellStyle}>exit slip</th>
            <th style={cellStyle}>real P&L</th>
            <th style={cellStyle}>edge erosion</th>
            <th style={cellStyle}>latency</th>
            <th style={cellStyle}>fill ts</th>
          </tr>
        </thead>
        <tbody>
          {records.map((r) => (
            <tr
              key={r.id}
              onClick={() => onSelect(r.id)}
              style={{ cursor: "pointer" }}
            >
              <td style={cellStyle}>#{r.id}</td>
              <td style={cellStyle}>{r.status}</td>
              <td style={cellStyle}>{r.strategy}</td>
              <td style={cellStyle}>{r.symbol}</td>
              <td style={cellStyle}>{r.side}</td>
              <td style={cellStyle}>{fmt(r.entry_slippage_pts)}</td>
              <td style={cellStyle}>{fmt(r.exit_slippage_pts)}</td>
              <td style={cellStyle}>{fmt(r.real_pnl_pts)}</td>
              <td style={cellStyle}>{fmt(r.slippage_total_pts)}</td>
              <td style={cellStyle}>
                {r.total_latency_ms !== null ? `${r.total_latency_ms} ms` : "—"}
              </td>
              <td style={cellStyle}>
                {r.fill_ts ? new Date(r.fill_ts).toLocaleTimeString() : "—"}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function RecordDetailModal({
  record,
  onClose,
}: {
  record: PartnerSlippageRecordFull;
  onClose: () => void;
}) {
  return (
    <div
      role="dialog"
      aria-modal="true"
      onClick={onClose}
      style={{
        position: "fixed",
        inset: 0,
        background: "rgba(0,0,0,0.75)",
        display: "flex",
        justifyContent: "center",
        alignItems: "flex-start",
        padding: "2rem 1rem",
        zIndex: 100,
      }}
    >
      <div
        onClick={(e) => e.stopPropagation()}
        style={{
          background: "var(--bg)",
          border: "1px solid var(--border)",
          maxWidth: 820,
          width: "100%",
          maxHeight: "85vh",
          overflow: "auto",
          padding: "1.25rem 1.5rem",
        }}
      >
        <div
          style={{
            display: "flex",
            justifyContent: "space-between",
            alignItems: "center",
            marginBottom: "0.75rem",
          }}
        >
          <h2 style={{ margin: 0, fontSize: "1.05rem" }}>
            record #{record.id} · {record.strategy} · {record.symbol}{" "}
            {record.side}
          </h2>
          <button
            onClick={onClose}
            className="btn"
            style={{ fontSize: "0.78rem", padding: "0.3rem 0.6rem" }}
          >
            close
          </button>
        </div>
        <div style={{ fontSize: "0.8rem", lineHeight: 1.5 }}>
          <p>
            <span className="dim">expected entry:</span>{" "}
            {record.expected_entry_price} ·{" "}
            <span className="dim">actual:</span>{" "}
            {record.actual_entry_price ?? "—"} ·{" "}
            <span className="dim">slip:</span> {fmt(record.entry_slippage_pts)}{" "}
            pts
          </p>
          <p>
            <span className="dim">stops:</span> hard{" "}
            {record.hard_stop_distance_pts}pt · trail{" "}
            {record.trailing_stop_distance_pts}pt ·{" "}
            <span className="dim">early stop:</span>{" "}
            {record.early_stop_condition || "—"}
          </p>
          <p>
            <span className="dim">exit:</span> {record.exit_type ?? "—"} @{" "}
            {record.actual_exit_price ?? "—"} ·{" "}
            <span className="dim">slip:</span> {fmt(record.exit_slippage_pts)}{" "}
            pts ·<span className="dim"> peak:</span>{" "}
            {record.peak_price ?? "—"}
          </p>
          <p>
            <span className="dim">P&L:</span> strategy{" "}
            {fmt(record.strategy_pnl_pts)} / real {fmt(record.real_pnl_pts)} =
            erosion {fmt(record.slippage_total_pts)} pts (
            {fmt(record.slippage_total_dollars)} USD)
          </p>
          <p>
            <span className="dim">latency components (ms):</span> signal{" "}
            {record.signal_latency_ms ?? "—"} · submit{" "}
            {record.submit_latency_ms ?? "—"} · ack{" "}
            {record.broker_ack_latency_ms ?? "—"} · fill{" "}
            {record.fill_latency_ms ?? "—"} · total{" "}
            {record.total_latency_ms ?? "—"}
          </p>
          <details style={{ marginTop: "0.6rem" }}>
            <summary className="dim" style={{ cursor: "pointer" }}>
              raw broker fill JSON
            </summary>
            <pre
              style={{
                background: "var(--bg-elev, #111)",
                padding: "0.6rem",
                marginTop: "0.4rem",
                overflowX: "auto",
                fontSize: "0.72rem",
              }}
            >
              {record.broker_fill_response
                ? JSON.stringify(record.broker_fill_response, null, 2)
                : "(none captured)"}
            </pre>
          </details>
          <details>
            <summary className="dim" style={{ cursor: "pointer" }}>
              raw broker close JSON
            </summary>
            <pre
              style={{
                background: "var(--bg-elev, #111)",
                padding: "0.6rem",
                marginTop: "0.4rem",
                overflowX: "auto",
                fontSize: "0.72rem",
              }}
            >
              {record.broker_close_response
                ? JSON.stringify(record.broker_close_response, null, 2)
                : "(none captured)"}
            </pre>
          </details>
        </div>
      </div>
    </div>
  );
}

function fmt(v: number | null | undefined): string {
  if (v === null || v === undefined) return "—";
  return v.toFixed(2);
}
