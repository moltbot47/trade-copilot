"use client";

import { useEffect, useState } from "react";
import { api } from "@/lib/api";
import { useWebSocket } from "@/hooks/useWebSocket";
import EmailGate from "@/components/EmailGate";
import type { ConnectResponse, AccountState } from "@/lib/types";
import type { AccountEvent } from "@/lib/ws-types";

export default function ConnectPage() {
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [showPassword, setShowPassword] = useState(false);
  const [server, setServer] = useState("GENFX");
  const [env, setEnv] = useState<"demo" | "live">("demo");
  const [loading, setLoading] = useState(false);
  const [result, setResult] = useState<ConnectResponse | null>(null);
  const [error, setError] = useState<string | null>(null);

  const [account, setAccount] = useState<AccountState | null>(null);
  const [accLoading, setAccLoading] = useState(true);
  const [accError, setAccError] = useState<string | null>(null);

  const ws = useWebSocket();

  useEffect(() => {
    let mounted = true;
    api
      .getAccountState()
      .then((a) => {
        if (mounted) setAccount(a);
      })
      .catch((e: Error) => {
        if (mounted) setAccError(e.message);
      })
      .finally(() => mounted && setAccLoading(false));
    return () => {
      mounted = false;
    };
  }, []);

  // Live account updates — balance/equity/PNL push as they change.
  useEffect(() => {
    const off = ws.subscribe<AccountEvent>("account", (payload) => {
      setAccount((curr) => ({ ...(curr || {}), ...payload }));
      setAccError(null);
    });
    return off;
  }, [ws.subscribe]);

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    // Confirmation step for live (real money)
    if (env === "live") {
      const ok = window.confirm(
        "⚠️  LIVE ACCOUNT\n\n" +
        "You are about to connect a LIVE TradeLocker account. " +
        "Strategy bots that execute on this connection will trade with REAL MONEY.\n\n" +
        "Are you sure you want to continue?"
      );
      if (!ok) return;
    }
    setError(null);
    setResult(null);
    setLoading(true);
    try {
      const res = await api.connectTradeLocker(email, password, server, env);
      setResult(res);
      // refresh account
      try {
        const a = await api.getAccountState();
        setAccount(a);
        setAccError(null);
      } catch (err) {
        // ignore
      }
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setLoading(false);
    }
  };

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: "1.5rem" }}>
      <EmailGate />

      <header>
        <div className="dim" style={{ fontSize: "0.85rem" }}>
          {">"} broker link
        </div>
        <h1 style={{ marginTop: "0.25rem" }}>connect tradelocker</h1>
        <p className="dim">
          We use your Genesis FX TradeLocker credentials to open a session. We
          never store your password in plaintext.
        </p>
      </header>

      <div
        style={{
          display: "grid",
          gridTemplateColumns: "repeat(auto-fit, minmax(300px, 1fr))",
          gap: "1.25rem",
        }}
      >
        <form
          onSubmit={submit}
          className="card"
          style={{ display: "flex", flexDirection: "column", gap: "0.75rem" }}
          aria-describedby={error ? "tl-form-error" : undefined}
          noValidate
        >
          <h2 style={{ marginTop: 0 }} className="accent">
            credentials
          </h2>
          <div>
            <label htmlFor="tl-email">tradelocker email</label>
            <input
              id="tl-email"
              type="email"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              required
              aria-required="true"
              autoComplete="username"
            />
          </div>
          <div>
            <label htmlFor="tl-password">password</label>
            <div style={{ position: "relative" }}>
              <input
                id="tl-password"
                type={showPassword ? "text" : "password"}
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                required
                aria-required="true"
                autoComplete="current-password"
                style={{ paddingRight: "4.5rem" }}
              />
              <button
                type="button"
                onClick={() => setShowPassword((v) => !v)}
                aria-label={showPassword ? "hide password" : "show password"}
                aria-pressed={showPassword}
                aria-controls="tl-password"
                style={{
                  position: "absolute",
                  right: "0.4rem",
                  top: "50%",
                  transform: "translateY(-50%)",
                  background: "transparent",
                  border: "1px solid var(--border-strong)",
                  color: "var(--text)",
                  fontSize: "0.7rem",
                  padding: "0.4rem 0.55rem",
                  cursor: "pointer",
                  textTransform: "uppercase",
                  letterSpacing: "0.05em",
                  minHeight: 36,
                  minWidth: 48,
                }}
              >
                {showPassword ? "hide" : "show"}
              </button>
            </div>
          </div>
          <div>
            <label htmlFor="tl-server">server</label>
            <input
              id="tl-server"
              type="text"
              value={server}
              onChange={(e) => setServer(e.target.value)}
              placeholder="GENFX"
              required
              aria-required="true"
              autoComplete="off"
              aria-describedby="tl-server-hint"
              pattern="[A-Za-z0-9_\-]+"
              maxLength={32}
            />
            <div id="tl-server-hint" className="dim" style={{ fontSize: "0.75rem", marginTop: "0.25rem" }}>
              Genesis FX uses <code>GENFX</code>
            </div>
          </div>
          <div>
            <label htmlFor="tl-env">environment</label>
            <select
              id="tl-env"
              value={env}
              onChange={(e) => setEnv(e.target.value as "demo" | "live")}
              aria-describedby="tl-env-hint"
            >
              <option value="demo">demo (paper money)</option>
              <option value="live">live (real money)</option>
            </select>
            <div id="tl-env-hint" className="dim" style={{ fontSize: "0.75rem", marginTop: "0.25rem" }}>
              live executes real-money trades — confirmation required.
            </div>
          </div>
          <div>
            <button type="submit" className="btn btn-primary" disabled={loading}>
              {loading ? "connecting..." : "connect"}
            </button>
          </div>
          {error && (
            <p
              id="tl-form-error"
              role="alert"
              aria-live="assertive"
              className="danger"
              style={{ margin: 0 }}
            >
              error: {error}
            </p>
          )}
          {result?.success && (
            <p
              role="status"
              aria-live="polite"
              className="accent"
              style={{ margin: 0 }}
            >
              connected{result.account_id ? ` — ${result.account_id}` : ""}
            </p>
          )}
        </form>

        <div className="card">
          <header style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: "0.75rem" }}>
            <h2 style={{ margin: 0 }} className="accent">current session</h2>
            {account?.connected && (
              <span style={{
                background: "var(--accent)",
                color: "#000",
                padding: "0.2rem 0.6rem",
                fontSize: "0.72rem",
                fontWeight: 700,
                borderRadius: 2,
                letterSpacing: "0.05em",
              }}>
                ● CONNECTED
              </span>
            )}
            {account && !account.connected && (
              <span style={{
                background: "var(--text-dim)",
                color: "#000",
                padding: "0.2rem 0.6rem",
                fontSize: "0.72rem",
                fontWeight: 700,
                borderRadius: 2,
              }}>
                ○ DISCONNECTED
              </span>
            )}
          </header>
          {accLoading && <p className="dim">loading...</p>}
          {accError && (
            <p className="dim" style={{ fontSize: "0.85rem" }}>
              {accError === "Backend offline"
                ? "backend offline"
                : "no session — connect above"}
            </p>
          )}
          {account?.connected && (
            <ul style={{ listStyle: "none", padding: 0, margin: 0, fontSize: "0.92rem", lineHeight: 1.7 }}>
              <li>
                <span className="dim">account: </span>
                <span className="accent">{account.account_id || "—"}</span>
              </li>
              <li>
                <span className="dim">server: </span>
                {account.server || "—"}
                {" "}
                <span className="dim" style={{ fontSize: "0.78rem" }}>
                  ({account.env})
                </span>
              </li>
              <li>
                <span className="dim">balance: </span>
                <span style={{ fontWeight: 600 }}>
                  ${account.balance?.toFixed(2) ?? "—"} {account.currency || ""}
                </span>
              </li>
              <li>
                <span className="dim">equity: </span>
                ${account.equity?.toFixed(2) ?? "—"}
              </li>
              <li>
                <span className="dim">available: </span>
                ${account.available_funds?.toFixed(2) ?? "—"}
              </li>
              <li>
                <span className="dim">open pnl: </span>
                <span
                  style={{
                    color:
                      (account.open_pnl ?? 0) >= 0
                        ? "var(--accent)"
                        : "var(--danger)",
                  }}
                >
                  ${account.open_pnl?.toFixed(2) ?? "—"}
                </span>
              </li>
              <li>
                <span className="dim">today net: </span>
                <span
                  style={{
                    color:
                      (account.today_net ?? 0) >= 0
                        ? "var(--accent)"
                        : "var(--danger)",
                  }}
                >
                  ${account.today_net?.toFixed(2) ?? "—"}
                </span>
              </li>
              <li>
                <span className="dim">open positions: </span>
                {account.positions_count ?? 0}
              </li>
            </ul>
          )}
        </div>
      </div>
    </div>
  );
}
