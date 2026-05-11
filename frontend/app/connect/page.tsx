"use client";

import { useEffect, useState } from "react";
import { api, ApiError, getUserEmail } from "@/lib/api";
import { useWebSocket } from "@/hooks/useWebSocket";
import EmailGate from "@/components/EmailGate";
import TinyAccountAdvisor from "@/components/TinyAccountAdvisor";
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

  // When the broker returns >1 linked accounts, we surface a picker here
  // and re-submit with the chosen account_id. Cleared on success and
  // re-fetched if the user wipes the form.
  type AccountCandidate = {
    account_id: string;
    acc_num: string;
    name?: string | null;
    balance?: number;
    currency?: string | null;
    status?: string | null;
    type?: string | null;
    is_current?: boolean;
  };
  const [accountCandidates, setAccountCandidates] = useState<AccountCandidate[] | null>(null);

  // Switch-account flow on the already-linked session. Lists all accounts
  // via the saved TradeLocker token (no password re-prompt), lets the user
  // swap to a different one in one click.
  const [switcherOpen, setSwitcherOpen] = useState(false);
  const [switcherAccounts, setSwitcherAccounts] = useState<AccountCandidate[] | null>(null);
  const [switcherLoading, setSwitcherLoading] = useState(false);
  const [switcherErr, setSwitcherErr] = useState<string | null>(null);

  const openSwitcher = async () => {
    setSwitcherOpen(true);
    setSwitcherLoading(true);
    setSwitcherErr(null);
    try {
      const data = await api.listTradeLockerAccounts();
      setSwitcherAccounts(data.accounts as AccountCandidate[]);
    } catch (err) {
      setSwitcherErr(friendlyError(err));
    } finally {
      setSwitcherLoading(false);
    }
  };

  const closeSwitcher = () => {
    setSwitcherOpen(false);
    setSwitcherAccounts(null);
    setSwitcherErr(null);
  };

  const doSwitch = async (accountId: string) => {
    setSwitcherLoading(true);
    setSwitcherErr(null);
    try {
      await api.switchTradeLockerAccount(accountId);
      // Refresh the current-session card with the new account.
      const a = await api.getAccountState();
      setAccount(a);
      closeSwitcher();
    } catch (err) {
      setSwitcherErr(friendlyError(err));
      setSwitcherLoading(false);
    }
  };

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

  const doConnect = async (accountId?: string) => {
    setError(null);
    setResult(null);
    setLoading(true);
    try {
      const res = await api.connectTradeLocker(
        email,
        password,
        server,
        env,
        accountId,
      );
      setResult(res);
      setAccountCandidates(null); // dismiss picker on success
      try {
        const a = await api.getAccountState();
        setAccount(a);
        setAccError(null);
      } catch {
        // ignore — we'll show whatever the WS pushes next
      }
    } catch (err) {
      // 409 with code=select_account → render the account picker rather
      // than treating it as a flat error.
      if (
        err instanceof ApiError &&
        err.status === 409 &&
        typeof err.detail === "object" &&
        err.detail !== null &&
        (err.detail as { code?: string }).code === "select_account"
      ) {
        const candidates = (err.detail as { accounts?: AccountCandidate[] }).accounts ?? [];
        setAccountCandidates(candidates);
        // Clear any stale error message so the picker stands alone.
        setError(null);
      } else {
        setError(friendlyError(err));
      }
    } finally {
      setLoading(false);
    }
  };

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
    await doConnect();
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
          {accountCandidates && accountCandidates.length > 0 && (
            <div
              role="region"
              aria-labelledby="picker-heading"
              style={{
                padding: "0.85rem 1rem",
                border: "1px solid var(--warn)",
                background: "rgba(255, 200, 0, 0.04)",
                display: "flex",
                flexDirection: "column",
                gap: "0.6rem",
              }}
            >
              <div
                id="picker-heading"
                style={{ display: "flex", alignItems: "baseline", gap: "0.5rem" }}
              >
                <span style={{ color: "var(--warn)", fontWeight: 700 }}>
                  ⚠ pick an account
                </span>
                <span className="dim" style={{ fontSize: "0.85rem" }}>
                  Your TradeLocker login has {accountCandidates.length} accounts.
                  Choose which one to link.
                </span>
              </div>
              <div
                style={{
                  display: "flex",
                  flexDirection: "column",
                  gap: "0.4rem",
                }}
              >
                {accountCandidates.map((a) => {
                  const inactive = a.status && a.status !== "ACTIVE";
                  return (
                    <button
                      key={a.account_id}
                      type="button"
                      onClick={() => doConnect(a.account_id)}
                      disabled={loading || !!inactive}
                      style={{
                        padding: "0.5rem 0.75rem",
                        display: "grid",
                        gridTemplateColumns: "minmax(80px,110px) 1fr auto",
                        gap: "0.6rem",
                        alignItems: "center",
                        textAlign: "left",
                        background: inactive
                          ? "transparent"
                          : "rgba(255,255,255,0.02)",
                        border: "1px solid var(--accent-dim)",
                        cursor: inactive ? "not-allowed" : "pointer",
                        color: inactive ? "var(--dim)" : "inherit",
                      }}
                    >
                      <span className="accent" style={{ fontWeight: 700 }}>
                        {a.account_id}
                      </span>
                      <span style={{ fontSize: "0.85rem" }}>
                        {a.name ?? "—"}
                        {a.type ? (
                          <span className="dim" style={{ marginLeft: "0.5rem", fontSize: "0.75rem" }}>
                            {a.type}
                          </span>
                        ) : null}
                        {a.status && a.status !== "ACTIVE" ? (
                          <span
                            style={{
                              marginLeft: "0.5rem",
                              fontSize: "0.7rem",
                              color: "var(--warn)",
                              letterSpacing: 1,
                            }}
                          >
                            {a.status}
                          </span>
                        ) : null}
                      </span>
                      <span
                        style={{
                          fontFamily: "monospace",
                          fontSize: "0.85rem",
                          color: "var(--accent)",
                        }}
                      >
                        {a.currency ?? "USD"}{" "}
                        {(a.balance ?? 0).toFixed(2)}
                      </span>
                    </button>
                  );
                })}
              </div>
              <button
                type="button"
                onClick={() => setAccountCandidates(null)}
                className="btn"
                style={{ padding: "0.4rem 0.8rem", fontSize: "0.8rem", alignSelf: "flex-start" }}
              >
                cancel
              </button>
            </div>
          )}

          {(result?.success || result?.status === "connected") && (
            <div
              role="status"
              aria-live="polite"
              style={{
                margin: 0,
                padding: "0.85rem 1rem",
                border: "1px solid var(--accent)",
                background: "rgba(0, 200, 83, 0.05)",
                display: "flex",
                flexDirection: "column",
                gap: "0.5rem",
              }}
            >
              <span className="accent" style={{ fontWeight: 700 }}>
                ✓ connected
                {result.account_id
                  ? ` — ${result.account_id}`
                  : result.detail
                    ? ` — ${result.detail}`
                    : ""}
              </span>
              <span className="dim" style={{ fontSize: "0.85rem" }}>
                Next: pick a bot to start trading.
              </span>
              <div style={{ display: "flex", gap: "0.5rem", flexWrap: "wrap" }}>
                <a
                  href="/bots"
                  className="btn"
                  style={{
                    padding: "0.55rem 1rem",
                    background: "var(--accent)",
                    color: "var(--bg)",
                    border: "none",
                    textDecoration: "none",
                    fontWeight: 700,
                  }}
                >
                  pick a bot →
                </a>
                <a
                  href="/settings/notifications"
                  className="btn"
                  style={{
                    padding: "0.55rem 1rem",
                    textDecoration: "none",
                  }}
                >
                  set up Discord
                </a>
              </div>
            </div>
          )}
        </form>

        {/* Render the advisor once we have a live broker session — either we
            just connected successfully, or the user is revisiting /connect
            with an already-linked account (account?.connected). */}
        {(result?.success || result?.status === "connected" || account?.connected) && (
          <TinyAccountAdvisor />
        )}

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

          {/* Switch-account control. Visible only when already linked.
              Clicking it lists every account on the user's TradeLocker
              session and lets them swap with one click — no password
              re-entry. */}
          {account?.connected && (
            <div style={{ marginTop: "0.85rem" }}>
              {!switcherOpen && (
                <button
                  type="button"
                  onClick={openSwitcher}
                  className="btn"
                  style={{
                    padding: "0.4rem 0.85rem",
                    fontSize: "0.82rem",
                    cursor: "pointer",
                  }}
                >
                  switch account ↻
                </button>
              )}

              {switcherOpen && (
                <div
                  role="region"
                  aria-labelledby="switcher-heading"
                  style={{
                    marginTop: "0.5rem",
                    padding: "0.6rem 0.75rem",
                    border: "1px solid var(--accent-dim)",
                    background: "rgba(255,255,255,0.02)",
                    display: "flex",
                    flexDirection: "column",
                    gap: "0.5rem",
                  }}
                >
                  <div
                    id="switcher-heading"
                    style={{
                      display: "flex",
                      justifyContent: "space-between",
                      alignItems: "baseline",
                    }}
                  >
                    <span className="accent" style={{ fontWeight: 700, fontSize: "0.88rem" }}>
                      pick a different account
                    </span>
                    <button
                      type="button"
                      onClick={closeSwitcher}
                      aria-label="Close"
                      className="dim"
                      style={{
                        background: "transparent",
                        border: "none",
                        cursor: "pointer",
                        fontSize: "1rem",
                      }}
                    >
                      ×
                    </button>
                  </div>

                  {switcherLoading && (
                    <p className="dim" style={{ fontSize: "0.82rem", margin: 0 }}>
                      loading accounts…
                    </p>
                  )}

                  {switcherErr && (
                    <p role="alert" className="danger" style={{ fontSize: "0.82rem", margin: 0 }}>
                      {switcherErr}
                    </p>
                  )}

                  {switcherAccounts && switcherAccounts.length > 0 && (
                    <div style={{ display: "flex", flexDirection: "column", gap: "0.35rem" }}>
                      {switcherAccounts.map((a) => {
                        const inactive = a.status && a.status !== "ACTIVE";
                        const active = a.is_current;
                        return (
                          <button
                            key={a.account_id}
                            type="button"
                            onClick={() => !active && !inactive && doSwitch(a.account_id)}
                            disabled={switcherLoading || !!inactive || !!active}
                            style={{
                              padding: "0.4rem 0.6rem",
                              display: "grid",
                              gridTemplateColumns: "minmax(70px,90px) 1fr auto",
                              gap: "0.5rem",
                              alignItems: "center",
                              textAlign: "left",
                              background: active ? "rgba(0, 200, 83, 0.07)" : "transparent",
                              border: `1px solid ${
                                active ? "var(--accent)" : "var(--accent-dim)"
                              }`,
                              cursor:
                                inactive || active ? "default" : "pointer",
                              color: inactive ? "var(--dim)" : "inherit",
                              fontSize: "0.85rem",
                            }}
                          >
                            <span className="accent" style={{ fontWeight: 700 }}>
                              {a.account_id}
                            </span>
                            <span>
                              {a.name ? a.name.slice(0, 30) : "—"}
                              {active ? (
                                <span
                                  style={{
                                    marginLeft: "0.5rem",
                                    fontSize: "0.7rem",
                                    color: "var(--accent)",
                                    letterSpacing: 1,
                                  }}
                                >
                                  CURRENT
                                </span>
                              ) : null}
                              {a.status && a.status !== "ACTIVE" ? (
                                <span
                                  style={{
                                    marginLeft: "0.5rem",
                                    fontSize: "0.7rem",
                                    color: "var(--warn)",
                                    letterSpacing: 1,
                                  }}
                                >
                                  {a.status}
                                </span>
                              ) : null}
                            </span>
                            <span
                              style={{
                                fontFamily: "monospace",
                                color: "var(--accent)",
                              }}
                            >
                              ${(a.balance ?? 0).toFixed(2)}
                            </span>
                          </button>
                        );
                      })}
                    </div>
                  )}
                </div>
              )}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
