"use client";

import { useEffect, useState } from "react";
import { api, ApiError, getUserEmail } from "@/lib/api";
import { useWebSocket } from "@/hooks/useWebSocket";
import EmailGate from "@/components/EmailGate";
import type { ConnectResponse, AccountState } from "@/lib/types";
import type { AccountEvent } from "@/lib/ws-types";

/**
 * Map an unknown error into copy that's friendly for non-technical users.
 * Tightly coupled to ApiError.kind from lib/api.ts.
 */
function friendlyError(err: unknown): string {
  if (err instanceof ApiError) {
    switch (err.kind) {
      case "network":
        return "Server unreachable. Try again.";
      case "auth":
        return "Session expired. Refresh and try again.";
      case "validation":
        // Body detail is already user-facing — surface it verbatim.
        return err.message || "Invalid email, password, or server.";
      case "server":
        return "Server error. Try again in a moment.";
      default:
        return err.message || "Something went wrong.";
    }
  }
  return (err as Error)?.message || "Something went wrong.";
}

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

    // Initial load: try to fetch account state. If we get a 401, the cookie
    // has likely expired in the user's browser even though localStorage still
    // says they're "signed in." Silently re-issue the cookie via login() and
    // retry once before giving up.
    const loadAccount = async () => {
      try {
        const a = await api.getAccountState();
        if (mounted) {
          setAccount(a);
          setAccError(null);
        }
      } catch (err) {
        if (err instanceof ApiError && err.kind === "auth") {
          const cachedEmail = getUserEmail();
          if (cachedEmail) {
            try {
              await api.login(cachedEmail);
              const a = await api.getAccountState();
              if (mounted) {
                setAccount(a);
                setAccError(null);
              }
              return;
            } catch (retryErr) {
              if (mounted) setAccError(friendlyError(retryErr));
              return;
            }
          }
        }
        if (mounted) setAccError(friendlyError(err));
      } finally {
        if (mounted) setAccLoading(false);
      }
    };

    loadAccount();

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
        // ignore — we'll show whatever the WS pushes next
      }
    } catch (err) {
      setError(friendlyError(err));
    } finally {
      setLoading(false);
    }
  };

  // Account-card status copy: differentiate "we have no session yet" from
  // "the network failed" from "the server returned an error."
  const sessionStatusText = (() => {
    if (!accError) return null;
    // ApiError messages we set above are already friendly.
    if (
      accError === "Server unreachable. Try again." ||
      accError === "Cannot reach the server. Check your connection."
    ) {
      return "server unreachable — try again";
    }
    if (accError === "Session expired. Refresh and try again.") {
      return "session expired — refresh and sign in";
    }
    if (accError === "Server error. Try again in a moment.") {
      return "server error — try again in a moment";
    }
    return "no session — connect above";
  })();

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
          {(result?.success || result?.status === "connected") && (
            <p
              role="status"
              aria-live="polite"
              className="accent"
              style={{ margin: 0 }}
            >
              connected
              {result.account_id
                ? ` — ${result.account_id}`
                : result.detail
                  ? ` — ${result.detail}`
                  : ""}
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
          {sessionStatusText && (
            <p className="dim" style={{ fontSize: "0.85rem" }}>
              {sessionStatusText}
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
