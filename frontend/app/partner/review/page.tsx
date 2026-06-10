"use client";

/**
 * Owner-side partner onboarding console.
 *
 *   - Generate single-use invite links to send a partner
 *   - Review pending uploads (metadata + AST safety verdict + source)
 *   - Approve → issues a scoped VIEWER grant + a registry-dispatched bot,
 *     then point the owner at the isolation harness to bind + start it
 *   - Reject with a reason
 *
 * Auth: the standard session cookie via api.ts. Owner-only endpoints 401
 * without it.
 */
import { useCallback, useEffect, useMemo, useState } from "react";
import Link from "next/link";
import { api } from "@/lib/api";
import type { PartnerInvite, PartnerSubmission } from "@/lib/types";

const C = {
  bg: "#0a0a0a",
  card: "#111",
  border: "#2a2a2a",
  text: "#e5e5e5",
  dim: "#888",
  accent: "#00ff41",
  warn: "#ffaa00",
  danger: "#ff3344",
};
const mono = "'SF Mono', 'Fira Code', Menlo, Consolas, monospace";

type Account = Awaited<ReturnType<typeof api.listAccounts>>[number];

export default function PartnerReviewPage() {
  const [invites, setInvites] = useState<PartnerInvite[]>([]);
  const [subs, setSubs] = useState<PartnerSubmission[]>([]);
  const [accounts, setAccounts] = useState<Account[]>([]);
  const [selected, setSelected] = useState<PartnerSubmission | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [copied, setCopied] = useState<number | null>(null);

  const ownerAccounts = useMemo(
    () => accounts.filter((a) => a.role === "owner"),
    [accounts],
  );

  const load = useCallback(async () => {
    try {
      const [inv, sub, acc] = await Promise.all([
        api.listPartnerInvites(),
        api.listPartnerSubmissions(),
        api.listAccounts(),
      ]);
      setInvites(inv);
      setSubs(sub.items);
      setAccounts(acc);
      setErr(null);
    } catch (e) {
      setErr((e as Error).message);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  const createInvite = async () => {
    const label = prompt("Label for this invite (e.g. 'Vladimir — NAS100 audit'):");
    if (label === null) return;

    // Offer instant-run on a demo account: the partner's strategy starts the
    // moment they submit, with no approval. Live accounts are never offered
    // here — you flip those to live yourself from the backend.
    const demoAccounts = accounts.filter(
      (a) => a.role === "owner" && a.env === "demo",
    );
    let trading_account_id: number | null = null;
    let auto_start = false;
    if (demoAccounts.length > 0) {
      const pick = prompt(
        "Run INSTANTLY on a demo account (no approval)? Enter an account ID, " +
          "or leave blank for the manual-approval flow:\n" +
          demoAccounts.map((a) => `  ${a.id} — ${a.label}`).join("\n"),
        String(demoAccounts[0].id),
      );
      if (pick && pick.trim()) {
        trading_account_id = parseInt(pick.trim(), 10);
        auto_start = true;
      }
    }

    setBusy(true);
    try {
      await api.createPartnerInvite({ label, trading_account_id, auto_start });
      await load();
    } catch (e) {
      alert("Create failed: " + (e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const copyLink = async (inv: PartnerInvite) => {
    const url = `${window.location.origin}${inv.url_path}`;
    await navigator.clipboard.writeText(url);
    setCopied(inv.id);
    setTimeout(() => setCopied(null), 1500);
  };

  const openSubmission = async (id: number) => {
    try {
      setSelected(await api.getPartnerSubmission(id));
    } catch (e) {
      alert("Load failed: " + (e as Error).message);
    }
  };

  const approve = async (sub: PartnerSubmission) => {
    if (ownerAccounts.length === 0) {
      alert("Register a trading account first (Accounts page) to grant viewer access.");
      return;
    }
    const idStr = prompt(
      "Account ID to grant VIEWER access on:\n" +
        ownerAccounts.map((a) => `  ${a.id} — ${a.label}`).join("\n"),
      String(ownerAccounts[0].id),
    );
    if (!idStr) return;
    const accountId = parseInt(idStr, 10);
    const expires = prompt("Grant expiry (YYYY-MM-DD, blank = never):", "");
    setBusy(true);
    try {
      const res = await api.approvePartnerSubmission(sub.id, {
        account_id: accountId,
        expires_at: expires ? new Date(expires).toISOString() : null,
        allowed_instruments_csv: sub.instruments_csv,
      });
      alert(
        `Approved ✓\nBot #${res.bot_id} (${res.bot_slug}) created.\n\n${res.next_step}`,
      );
      setSelected(null);
      await load();
    } catch (e) {
      alert("Approve failed: " + (e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const reject = async (sub: PartnerSubmission) => {
    const reason = prompt("Reason for rejection (shared in audit log):", "");
    if (reason === null) return;
    setBusy(true);
    try {
      await api.rejectPartnerSubmission(sub.id, reason);
      setSelected(null);
      await load();
    } catch (e) {
      alert("Reject failed: " + (e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const stateColor = (s: string) =>
    s === "active" || s === "approved"
      ? C.accent
      : s === "pending"
        ? C.warn
        : C.dim;

  const wrap: React.CSSProperties = {
    minHeight: "100vh",
    background: C.bg,
    color: C.text,
    fontFamily: mono,
    padding: 24,
  };
  const card: React.CSSProperties = {
    background: C.card,
    border: `1px solid ${C.border}`,
    borderRadius: 8,
    padding: 20,
    marginBottom: 20,
  };
  const btn = (bg: string, fg = C.bg): React.CSSProperties => ({
    background: bg,
    color: fg,
    border: "none",
    borderRadius: 4,
    padding: "8px 14px",
    fontFamily: mono,
    fontWeight: 600,
    cursor: "pointer",
  });
  const th: React.CSSProperties = {
    textAlign: "left",
    color: C.dim,
    fontSize: 11,
    textTransform: "uppercase",
    letterSpacing: 1,
    padding: "6px 10px",
    borderBottom: `1px solid ${C.border}`,
  };
  const td: React.CSSProperties = {
    padding: "8px 10px",
    borderBottom: `1px solid ${C.border}`,
    fontSize: 13,
  };

  return (
    <div style={wrap}>
      <div style={{ maxWidth: 1000, margin: "0 auto" }}>
        <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between" }}>
          <h1 style={{ color: C.accent, fontSize: 20 }}>Partner onboarding</h1>
          <Link href="/partner" style={{ color: C.dim, fontSize: 13 }}>
            ← partner dashboard
          </Link>
        </div>

        {err && <p style={{ color: C.danger }}>{err}</p>}

        {/* Invites */}
        <div style={card}>
          <div style={{ display: "flex", justifyContent: "space-between", marginBottom: 12 }}>
            <h2 style={{ fontSize: 15 }}>Invite links</h2>
            <button style={btn(C.accent)} onClick={createInvite} disabled={busy}>
              + New invite
            </button>
          </div>
          {invites.length === 0 ? (
            <p style={{ color: C.dim, fontSize: 13 }}>
              No invites yet. Create one and send the link to your partner.
            </p>
          ) : (
            <table style={{ width: "100%", borderCollapse: "collapse" }}>
              <thead>
                <tr>
                  <th style={th}>Label</th>
                  <th style={th}>Mode</th>
                  <th style={th}>State</th>
                  <th style={th}>Link</th>
                  <th style={th}></th>
                </tr>
              </thead>
              <tbody>
                {invites.map((inv) => (
                  <tr key={inv.id}>
                    <td style={td}>{inv.label || "—"}</td>
                    <td style={td}>
                      {inv.auto_start && inv.account_env === "demo" ? (
                        <span style={{ color: C.accent }}>
                          ⚡ instant · {inv.account_label} (demo)
                        </span>
                      ) : (
                        <span style={{ color: C.dim }}>manual approval</span>
                      )}
                    </td>
                    <td style={{ ...td, color: stateColor(inv.state) }}>{inv.state}</td>
                    <td style={{ ...td, color: C.dim }}>{inv.url_path}</td>
                    <td style={{ ...td, textAlign: "right" }}>
                      {inv.state === "active" && (
                        <>
                          <button style={btn(C.border, C.text)} onClick={() => copyLink(inv)}>
                            {copied === inv.id ? "Copied!" : "Copy"}
                          </button>{" "}
                          <button
                            style={btn(C.border, C.danger)}
                            onClick={async () => {
                              await api.revokePartnerInvite(inv.id);
                              load();
                            }}
                          >
                            Revoke
                          </button>
                        </>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>

        {/* Submissions */}
        <div style={card}>
          <h2 style={{ fontSize: 15, marginBottom: 12 }}>Submissions</h2>
          {subs.length === 0 ? (
            <p style={{ color: C.dim, fontSize: 13 }}>No submissions yet.</p>
          ) : (
            <table style={{ width: "100%", borderCollapse: "collapse" }}>
              <thead>
                <tr>
                  <th style={th}>Partner</th>
                  <th style={th}>Strategy</th>
                  <th style={th}>Delivery</th>
                  <th style={th}>Scan</th>
                  <th style={th}>Status</th>
                  <th style={th}></th>
                </tr>
              </thead>
              <tbody>
                {subs.map((s) => (
                  <tr key={s.id}>
                    <td style={td}>
                      {s.partner_name}
                      <div style={{ color: C.dim, fontSize: 11 }}>{s.partner_email}</div>
                    </td>
                    <td style={td}>{s.strategy_name}</td>
                    <td style={td}>{s.delivery_type}</td>
                    <td style={td}>
                      {s.delivery_type === "http" ? (
                        <span style={{ color: C.dim }}>n/a</span>
                      ) : s.ast_scan?.ok ? (
                        <span style={{ color: C.accent }}>✓ clean</span>
                      ) : (
                        <span style={{ color: C.danger }}>⚠ flagged</span>
                      )}
                    </td>
                    <td style={{ ...td, color: stateColor(s.status) }}>{s.status}</td>
                    <td style={{ ...td, textAlign: "right" }}>
                      <button style={btn(C.border, C.text)} onClick={() => openSubmission(s.id)}>
                        Review
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      </div>

      {/* Detail drawer */}
      {selected && (
        <div
          style={{
            position: "fixed",
            inset: 0,
            background: "rgba(0,0,0,0.7)",
            display: "flex",
            justifyContent: "flex-end",
          }}
          onClick={() => setSelected(null)}
        >
          <div
            style={{
              width: "min(640px, 100%)",
              height: "100%",
              background: C.card,
              borderLeft: `1px solid ${C.border}`,
              padding: 24,
              overflowY: "auto",
            }}
            onClick={(e) => e.stopPropagation()}
          >
            <div style={{ display: "flex", justifyContent: "space-between" }}>
              <h2 style={{ color: C.accent, fontSize: 17 }}>{selected.strategy_name}</h2>
              <button style={btn(C.border, C.text)} onClick={() => setSelected(null)}>
                ✕
              </button>
            </div>
            <p style={{ color: C.dim, fontSize: 13 }}>
              {selected.partner_name} · {selected.partner_email}
            </p>
            <dl style={{ fontSize: 13, lineHeight: 1.8 }}>
              <div>slug: <code style={{ color: C.accent }}>{selected.strategy_slug}</code></div>
              <div>delivery: {selected.delivery_type}</div>
              <div>instruments: {selected.instruments_csv}</div>
              <div>timeframe: {selected.timeframe}</div>
              {selected.endpoint_url && <div>endpoint: {selected.endpoint_url}</div>}
              {selected.params_json && <div>params: {selected.params_json}</div>}
            </dl>

            {selected.backtest_notes && (
              <>
                <div style={{ color: C.dim, fontSize: 12, marginTop: 12 }}>BACKTEST NOTES</div>
                <p style={{ fontSize: 13, whiteSpace: "pre-wrap" }}>{selected.backtest_notes}</p>
              </>
            )}

            {selected.ast_scan && (
              <>
                <div style={{ color: C.dim, fontSize: 12, marginTop: 12 }}>SAFETY SCAN</div>
                <p style={{ color: selected.ast_scan.ok ? C.accent : C.danger, fontSize: 13 }}>
                  {selected.ast_scan.ok ? "✓ passed" : "⚠ has blocking findings"}
                </p>
                {selected.ast_scan.findings.length > 0 && (
                  <ul style={{ paddingLeft: 18 }}>
                    {selected.ast_scan.findings.map((f, i) => (
                      <li key={i} style={{ color: f.level === "block" ? C.danger : C.warn, fontSize: 12 }}>
                        [{f.code}] {f.message}
                        {f.line ? ` (line ${f.line})` : ""}
                      </li>
                    ))}
                  </ul>
                )}
              </>
            )}

            {selected.source_code && (
              <>
                <div style={{ color: C.dim, fontSize: 12, marginTop: 12 }}>SOURCE</div>
                <pre
                  style={{
                    background: C.bg,
                    border: `1px solid ${C.border}`,
                    borderRadius: 4,
                    padding: 12,
                    fontSize: 12,
                    overflowX: "auto",
                    maxHeight: 320,
                  }}
                >
                  {selected.source_code}
                </pre>
              </>
            )}

            {selected.status === "pending" ? (
              <div style={{ display: "flex", gap: 10, marginTop: 20 }}>
                <button style={btn(C.accent)} onClick={() => approve(selected)} disabled={busy}>
                  Approve →
                </button>
                <button style={btn(C.danger)} onClick={() => reject(selected)} disabled={busy}>
                  Reject
                </button>
              </div>
            ) : (
              <p style={{ color: stateColor(selected.status), marginTop: 20 }}>
                {selected.status}
                {selected.rejection_reason ? ` — ${selected.rejection_reason}` : ""}
                {selected.approved_bot_id ? ` (bot #${selected.approved_bot_id})` : ""}
              </p>
            )}
          </div>
        </div>
      )}
    </div>
  );
}
