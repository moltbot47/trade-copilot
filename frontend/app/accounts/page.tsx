"use client";

import { useCallback, useEffect, useState } from "react";
import Link from "next/link";
import { api } from "@/lib/api";

type Account = Awaited<ReturnType<typeof api.listAccounts>>[number];
type Grant = Awaited<ReturnType<typeof api.listGrants>>[number];

export default function AccountsPage() {
  const [accounts, setAccounts] = useState<Account[] | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    try {
      const arr = await api.listAccounts();
      setAccounts(arr);
      setErr(null);
    } catch (e) {
      setErr((e as Error).message);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  const registerCurrent = async () => {
    const label = prompt("Label for this account (e.g. 'Main Genesis Demo'):");
    if (!label) return;
    setBusy(true);
    try {
      await api.registerAccount({ label });
      await load();
    } catch (e) {
      alert("Register failed: " + (e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const removeAccount = async (id: number) => {
    if (!confirm("Deactivate this account and revoke all grants?")) return;
    setBusy(true);
    try {
      await api.deactivateAccount(id);
      await load();
    } catch (e) {
      alert("Deactivate failed: " + (e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="dom-shell">
      <header style={{ marginBottom: "0.5rem" }}>
        <div className="dim dom-label-text">{">"} delegated trading accounts</div>
        <h1 style={{ color: "var(--accent)", marginTop: "0.25rem" }}>accounts</h1>
        <p className="dim" style={{ fontSize: "0.88rem" }}>
          Register a broker account, then invite an employee (by email — they
          must register and log in once first) with optional risk caps. The
          employee trades on your account without ever seeing your credentials.
        </p>
      </header>

      {err && (
        <div className="dom-result-toast" data-kind="err">
          {err}
        </div>
      )}

      <div style={{ display: "flex", gap: "0.5rem", flexWrap: "wrap" }}>
        <button
          className="dom-big-btn dom-buy"
          onClick={registerCurrent}
          disabled={busy}
          style={{ minHeight: 52, fontSize: "0.9rem" }}
        >
          + register my connected broker as an account
        </button>
        <Link
          href="/connect"
          className="dom-secondary-btn"
          style={{
            display: "inline-flex",
            alignItems: "center",
            padding: "0.7rem 1rem",
            textDecoration: "none",
          }}
        >
          connect broker first
        </Link>
      </div>

      {accounts === null && <p className="dim">loading…</p>}
      {accounts && accounts.length === 0 && (
        <p className="dim">
          No accounts yet. Click the button above (after connecting your broker
          on the connect page).
        </p>
      )}
      {accounts?.map((a) => (
        <AccountCard
          key={a.id}
          account={a}
          onRemove={() => removeAccount(a.id)}
          onChange={load}
          busy={busy}
        />
      ))}
    </div>
  );
}

function AccountCard({
  account,
  onRemove,
  onChange,
  busy,
}: {
  account: Account;
  onRemove: () => void;
  onChange: () => void;
  busy: boolean;
}) {
  const [grants, setGrants] = useState<Grant[] | null>(null);
  const [showGrants, setShowGrants] = useState(false);

  const loadGrants = useCallback(async () => {
    if (account.role !== "owner") {
      setGrants([]);
      return;
    }
    try {
      const g = await api.listGrants(account.id);
      setGrants(g);
    } catch (e) {
      console.warn("grants load error", e);
      setGrants([]);
    }
  }, [account]);

  useEffect(() => {
    if (showGrants && grants === null) loadGrants();
  }, [showGrants, grants, loadGrants]);

  return (
    <section className="dom-section">
      <div className="dom-section-header">
        <h2>{account.label}</h2>
        <span className="dim" style={{ fontSize: "0.78rem" }}>
          [{account.role}] {account.env}
        </span>
      </div>
      <div style={{ padding: "0.85rem 1rem", fontSize: "0.85rem" }}>
        <div className="dim">broker id: {account.tradelocker_account_id}</div>
        {account.role !== "owner" && account.owner_email && (
          <div className="dim">owned by {account.owner_email}</div>
        )}
      </div>
      {account.role === "owner" && (
        <div style={{ padding: "0 1rem 1rem" }}>
          <div style={{ display: "flex", gap: "0.5rem", flexWrap: "wrap" }}>
            <button
              className="dom-secondary-btn"
              onClick={() => setShowGrants((v) => !v)}
            >
              {showGrants ? "hide" : "show"} grants
            </button>
            <button
              className="dom-secondary-btn dom-flatten"
              onClick={onRemove}
              disabled={busy}
            >
              deactivate
            </button>
          </div>
          {showGrants && (
            <GrantsPanel
              accountId={account.id}
              grants={grants}
              onChange={() => {
                loadGrants();
                onChange();
              }}
            />
          )}
        </div>
      )}
    </section>
  );
}

function GrantsPanel({
  accountId,
  grants,
  onChange,
}: {
  accountId: number;
  grants: Grant[] | null;
  onChange: () => void;
}) {
  const [email, setEmail] = useState("");
  const [role, setRole] = useState<"trader" | "viewer">("trader");
  const [maxLot, setMaxLot] = useState("");
  const [maxLoss, setMaxLoss] = useState("");
  const [allowedInstr, setAllowedInstr] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  const submit = async () => {
    if (!email) return;
    setBusy(true);
    setErr(null);
    try {
      await api.createGrant(accountId, {
        grantee_email: email,
        role,
        max_lot_per_order: maxLot ? parseFloat(maxLot) : null,
        max_daily_loss_usd: maxLoss ? parseFloat(maxLoss) : null,
        allowed_instruments_csv: allowedInstr || null,
      });
      setEmail("");
      setMaxLot("");
      setMaxLoss("");
      setAllowedInstr("");
      onChange();
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const revoke = async (gid: number) => {
    if (!confirm("Revoke this grant?")) return;
    setBusy(true);
    try {
      await api.revokeGrant(accountId, gid);
      onChange();
    } catch (e) {
      alert("revoke failed: " + (e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const active = grants?.filter((g) => !g.revoked_at) ?? [];
  const revoked = grants?.filter((g) => g.revoked_at) ?? [];

  return (
    <div style={{ marginTop: "0.85rem" }}>
      {err && (
        <div className="dom-result-toast" data-kind="err">
          {err}
        </div>
      )}
      <div
        style={{
          display: "grid",
          gridTemplateColumns: "1fr",
          gap: "0.5rem",
          marginTop: "0.5rem",
        }}
      >
        <input
          className="dom-edit-input"
          placeholder="grantee email"
          type="email"
          inputMode="email"
          value={email}
          onChange={(e) => setEmail(e.target.value)}
        />
        <select
          className="dom-select"
          value={role}
          onChange={(e) => setRole(e.target.value as "trader" | "viewer")}
        >
          <option value="trader">trader (can trade)</option>
          <option value="viewer">viewer (read-only)</option>
        </select>
        <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: "0.5rem" }}>
          <input
            className="dom-edit-input"
            placeholder="max lot/order"
            type="number"
            inputMode="decimal"
            value={maxLot}
            onChange={(e) => setMaxLot(e.target.value)}
          />
          <input
            className="dom-edit-input"
            placeholder="daily loss cap $"
            type="number"
            inputMode="decimal"
            value={maxLoss}
            onChange={(e) => setMaxLoss(e.target.value)}
          />
        </div>
        <input
          className="dom-edit-input"
          placeholder="instruments (comma-sep, e.g. EURUSD,XAUUSD) or blank for any"
          value={allowedInstr}
          onChange={(e) => setAllowedInstr(e.target.value)}
        />
        <button
          className="dom-big-btn dom-buy"
          onClick={submit}
          disabled={busy || !email}
          style={{ minHeight: 48, fontSize: "0.9rem" }}
        >
          invite
        </button>
      </div>

      <div style={{ marginTop: "1rem" }}>
        <div className="dim dom-label-text">active</div>
        {active.length === 0 && (
          <p className="dim" style={{ fontSize: "0.85rem" }}>no active grants</p>
        )}
        {active.map((g) => (
          <div
            key={g.id}
            style={{
              padding: "0.6rem 0",
              borderBottom: "1px solid var(--border)",
              display: "flex",
              flexDirection: "column",
              gap: "0.3rem",
              fontSize: "0.85rem",
            }}
          >
            <div style={{ display: "flex", alignItems: "baseline", gap: "0.5rem" }}>
              <strong style={{ color: "var(--accent)" }}>{g.grantee_email}</strong>
              <span className="dim">[{g.role}]</span>
            </div>
            <div className="dim" style={{ fontSize: "0.75rem" }}>
              caps:{" "}
              {g.max_lot_per_order != null && <>max lot {g.max_lot_per_order} · </>}
              {g.max_daily_loss_usd != null && <>loss cap ${g.max_daily_loss_usd} · </>}
              {g.allowed_instruments_csv && <>symbols {g.allowed_instruments_csv}</>}
              {!g.max_lot_per_order && !g.max_daily_loss_usd && !g.allowed_instruments_csv &&
                "no caps"}
            </div>
            <button
              className="dom-secondary-btn dom-flatten"
              onClick={() => revoke(g.id)}
              style={{ alignSelf: "flex-start", padding: "0.4rem 0.85rem", minHeight: 36, fontSize: "0.78rem" }}
            >
              revoke
            </button>
          </div>
        ))}
      </div>

      {revoked.length > 0 && (
        <div style={{ marginTop: "1rem" }}>
          <div className="dim dom-label-text">revoked (history)</div>
          {revoked.map((g) => (
            <div
              key={g.id}
              style={{
                padding: "0.4rem 0",
                fontSize: "0.78rem",
                color: "var(--text-dim)",
              }}
            >
              {g.grantee_email} [{g.role}] revoked {g.revoked_at}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
