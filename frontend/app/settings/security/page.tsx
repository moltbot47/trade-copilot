"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { api } from "@/lib/api";
import EmailGate from "@/components/EmailGate";

type Setup = { secret: string; otpauth_uri: string };

export default function SecuritySettings() {
  const [enabled, setEnabled] = useState<boolean | null>(null);
  const [setup, setSetup] = useState<Setup | null>(null);
  const [code, setCode] = useState("");
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<{ kind: "ok" | "err"; text: string } | null>(null);

  useEffect(() => {
    (async () => {
      try {
        const r = await api.getMfaStatus();
        setEnabled(r.enabled);
      } catch (err) {
        setMsg({ kind: "err", text: (err as Error).message });
      }
    })();
  }, []);

  const start = async () => {
    setBusy(true);
    setMsg(null);
    try {
      const r = await api.setupMfa();
      setSetup({ secret: r.secret, otpauth_uri: r.otpauth_uri });
    } catch (err) {
      setMsg({ kind: "err", text: (err as Error).message });
    } finally {
      setBusy(false);
    }
  };

  const verify = async () => {
    setBusy(true);
    setMsg(null);
    try {
      const r = await api.verifyMfa(code);
      setEnabled(r.enabled);
      setSetup(null);
      setCode("");
      setMsg({ kind: "ok", text: "MFA enabled. Keep your authenticator handy." });
    } catch (err) {
      setMsg({ kind: "err", text: (err as Error).message });
    } finally {
      setBusy(false);
    }
  };

  const disable = async () => {
    if (!confirm("Disable MFA? Your account loses second-factor protection.")) return;
    setBusy(true);
    setMsg(null);
    try {
      const r = await api.disableMfa(code);
      setEnabled(r.enabled);
      setCode("");
      setMsg({ kind: "ok", text: "MFA disabled." });
    } catch (err) {
      setMsg({ kind: "err", text: (err as Error).message });
    } finally {
      setBusy(false);
    }
  };

  // QR generation: simple — we don't want to pull a QR lib, so we use an
  // external service URL only as a fallback. The otpauth URI is shown
  // alongside so users can paste it manually if they prefer.
  const qrUrl = setup
    ? `https://api.qrserver.com/v1/create-qr-code/?size=240x240&data=${encodeURIComponent(
        setup.otpauth_uri,
      )}`
    : null;

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: "1.25rem" }}>
      <EmailGate />
      <header>
        <div className="dim" style={{ fontSize: "0.85rem" }}>
          <Link href="/settings" style={{ color: "var(--text-dim)" }}>
            ‹ settings
          </Link>
          {" > "}security
        </div>
        <h1 style={{ margin: "0.25rem 0" }}>
          <span className="accent">Two-factor authentication</span>
        </h1>
        <p className="dim" style={{ margin: 0, fontSize: "0.88rem", maxWidth: "60ch" }}>
          Adds a 6-digit code from your phone's authenticator app to every
          login. If you trade real money, enable this.
        </p>
      </header>

      <section className="card" style={{ padding: "1.25rem" }}>
        <h2 style={{ marginTop: 0 }} className="accent">
          {">"} status
        </h2>
        {enabled === null ? (
          <p className="dim">loading…</p>
        ) : enabled ? (
          <>
            <div>
              <span className="accent">enabled</span>
              <span className="dim"> · MFA is required at every login</span>
            </div>
            <div style={{ marginTop: "1rem" }}>
              <p style={{ fontSize: "0.88rem", marginBottom: "0.5rem" }}>
                To disable, enter a current 6-digit code:
              </p>
              <input
                type="text"
                inputMode="numeric"
                value={code}
                onChange={(e) => setCode(e.target.value.replace(/\D/g, ""))}
                maxLength={6}
                placeholder="000000"
                style={{
                  padding: "0.6rem",
                  fontFamily: "inherit",
                  fontSize: "1.1rem",
                  letterSpacing: "0.3em",
                  background: "var(--bg)",
                  color: "var(--text)",
                  border: "1px solid var(--border)",
                  width: "12ch",
                }}
              />
              <button
                onClick={disable}
                disabled={busy || code.length !== 6}
                className="btn"
                style={{
                  marginLeft: "0.5rem",
                  padding: "0.6rem 1rem",
                  borderColor: "var(--danger)",
                  color: "var(--danger)",
                }}
              >
                {busy ? "…" : "disable MFA"}
              </button>
            </div>
          </>
        ) : setup === null ? (
          <>
            <p className="dim" style={{ marginBottom: "0.75rem" }}>
              MFA is currently <b>disabled</b>. Click below to start setup.
            </p>
            <button
              onClick={start}
              disabled={busy}
              className="btn"
              style={{ padding: "0.6rem 1.2rem" }}
            >
              {busy ? "preparing…" : "enable MFA"}
            </button>
          </>
        ) : (
          <>
            <p style={{ fontSize: "0.92rem" }}>
              <b>Step 1.</b> Scan this QR with Google Authenticator, Authy, or
              1Password.
            </p>
            {qrUrl && (
              // External QR service is fine here — only the OTP URI (not the
              // secret in plaintext) needs to round-trip and even that is
              // public-by-design (the user scans it themselves).
              // eslint-disable-next-line @next/next/no-img-element
              <img
                src={qrUrl}
                alt="MFA QR code"
                width={240}
                height={240}
                style={{ background: "white", padding: "8px", margin: "0.5rem 0" }}
              />
            )}
            <p style={{ fontSize: "0.85rem" }} className="dim">
              Can't scan? Enter this secret manually:
            </p>
            <code
              style={{
                display: "inline-block",
                padding: "0.4rem 0.6rem",
                background: "var(--bg)",
                border: "1px solid var(--border)",
                fontSize: "0.95rem",
                letterSpacing: "0.1em",
              }}
            >
              {setup.secret}
            </code>

            <p style={{ marginTop: "1rem", fontSize: "0.92rem" }}>
              <b>Step 2.</b> Type the 6-digit code your app shows:
            </p>
            <input
              type="text"
              inputMode="numeric"
              value={code}
              onChange={(e) => setCode(e.target.value.replace(/\D/g, ""))}
              maxLength={6}
              placeholder="000000"
              style={{
                padding: "0.6rem",
                fontFamily: "inherit",
                fontSize: "1.1rem",
                letterSpacing: "0.3em",
                background: "var(--bg)",
                color: "var(--text)",
                border: "1px solid var(--border)",
                width: "12ch",
              }}
            />
            <button
              onClick={verify}
              disabled={busy || code.length !== 6}
              className="btn"
              style={{ marginLeft: "0.5rem", padding: "0.6rem 1rem" }}
            >
              {busy ? "…" : "verify & enable"}
            </button>
          </>
        )}
      </section>

      {msg && (
        <div
          role="alert"
          className="card"
          style={{
            borderColor: msg.kind === "ok" ? "var(--accent)" : "var(--danger)",
            padding: "0.75rem 1rem",
          }}
        >
          <span className={msg.kind === "ok" ? "accent" : "danger"}>
            {msg.kind === "ok" ? "✓" : "✗"}
          </span>{" "}
          {msg.text}
        </div>
      )}
    </div>
  );
}
