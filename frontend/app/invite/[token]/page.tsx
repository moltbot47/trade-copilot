"use client";

/**
 * Public partner strategy upload page. Token-gated, no login required.
 *
 * The owner sends a partner this link (/invite/<token>). The partner fills
 * in who they are + how their strategy is delivered (upload a .py OR point
 * us at their HTTPS endpoint) and submits. A source upload is AST-scanned
 * server-side before it is stored; a failed scan bounces here with the
 * findings WITHOUT burning the single-use link, so they can fix + resubmit.
 *
 * No api.ts (which assumes an auth cookie) — this page talks to the public
 * invite routes with a bare fetch.
 */
import { useParams } from "next/navigation";
import { useCallback, useEffect, useState } from "react";

const BASE_URL =
  process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000";

type InviteInfo = {
  token: string;
  label: string;
  partner_name_hint: string | null;
  partner_email_hint: string | null;
  expires_at: string | null;
  account_label: string | null;
  account_env: string | null;
  instant_start: boolean;
};

type Finding = { level: string; code: string; message: string; line: number };

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

export default function InvitePage() {
  const params = useParams<{ token: string }>();
  const token = params?.token as string;

  const [info, setInfo] = useState<InviteInfo | null>(null);
  const [loadErr, setLoadErr] = useState<string | null>(null);

  const [delivery, setDelivery] = useState<"source" | "http">("source");
  const [partnerName, setPartnerName] = useState("");
  const [partnerEmail, setPartnerEmail] = useState("");
  const [strategyName, setStrategyName] = useState("");
  const [instruments, setInstruments] = useState("NAS100");
  const [timeframe, setTimeframe] = useState("1m");
  const [paramsJson, setParamsJson] = useState("");
  const [backtest, setBacktest] = useState("");
  const [file, setFile] = useState<File | null>(null);
  const [endpointUrl, setEndpointUrl] = useState("");
  const [endpointSecret, setEndpointSecret] = useState("");

  const [busy, setBusy] = useState(false);
  const [findings, setFindings] = useState<Finding[] | null>(null);
  const [errorMsg, setErrorMsg] = useState<string | null>(null);
  const [done, setDone] = useState<{ slug: string; running: boolean } | null>(
    null,
  );

  const load = useCallback(async () => {
    try {
      const r = await fetch(`${BASE_URL}/api/invite/${token}`);
      if (r.status === 404) {
        setLoadErr("This invite link is not valid.");
        return;
      }
      if (r.status === 410) {
        const b = await r.json().catch(() => ({}));
        setLoadErr(`This invite link is ${b.detail || "no longer usable"}.`);
        return;
      }
      if (!r.ok) {
        setLoadErr("Could not load this invite. Try again later.");
        return;
      }
      const data: InviteInfo = await r.json();
      setInfo(data);
      if (data.partner_name_hint) setPartnerName(data.partner_name_hint);
      if (data.partner_email_hint) setPartnerEmail(data.partner_email_hint);
    } catch {
      setLoadErr("Could not reach the server.");
    }
  }, [token]);

  useEffect(() => {
    if (token) load();
  }, [token, load]);

  const submit = async () => {
    setBusy(true);
    setFindings(null);
    setErrorMsg(null);
    try {
      const fd = new FormData();
      fd.set("partner_name", partnerName);
      fd.set("partner_email", partnerEmail);
      fd.set("strategy_name", strategyName);
      fd.set("delivery_type", delivery);
      fd.set("instruments_csv", instruments);
      fd.set("timeframe", timeframe);
      if (paramsJson.trim()) fd.set("params_json", paramsJson.trim());
      if (backtest.trim()) fd.set("backtest_notes", backtest.trim());
      if (delivery === "source") {
        if (!file) {
          setErrorMsg("Please choose a .py file.");
          setBusy(false);
          return;
        }
        fd.set("file", file);
      } else {
        fd.set("endpoint_url", endpointUrl.trim());
        fd.set("endpoint_secret", endpointSecret.trim());
      }

      const r = await fetch(`${BASE_URL}/api/invite/${token}/submit`, {
        method: "POST",
        body: fd,
      });

      if (r.status === 422) {
        const b = await r.json().catch(() => null);
        const d = b?.detail;
        setFindings(d?.findings || []);
        setErrorMsg(d?.message || "The strategy failed the safety scan.");
        return;
      }
      if (r.status === 410) {
        setErrorMsg("This invite link has already been used. Ask for a new one.");
        return;
      }
      if (!r.ok) {
        const b = await r.json().catch(() => null);
        setErrorMsg(
          typeof b?.detail === "string" ? b.detail : "Submission failed. Check your inputs.",
        );
        return;
      }
      const b = await r.json();
      setDone({ slug: b.strategy_slug, running: b.status === "running" });
    } catch {
      setErrorMsg("Could not reach the server.");
    } finally {
      setBusy(false);
    }
  };

  const page: React.CSSProperties = {
    minHeight: "100vh",
    background: C.bg,
    color: C.text,
    fontFamily: mono,
    padding: "32px 16px",
    display: "flex",
    justifyContent: "center",
  };
  const cardStyle: React.CSSProperties = {
    width: "100%",
    maxWidth: 680,
    background: C.card,
    border: `1px solid ${C.border}`,
    borderRadius: 8,
    padding: 28,
  };
  const label: React.CSSProperties = {
    display: "block",
    color: C.dim,
    fontSize: 12,
    textTransform: "uppercase",
    letterSpacing: 1,
    margin: "16px 0 6px",
  };
  const input: React.CSSProperties = {
    width: "100%",
    background: C.bg,
    color: C.text,
    border: `1px solid ${C.border}`,
    borderRadius: 4,
    padding: "10px 12px",
    fontFamily: mono,
    fontSize: 14,
    boxSizing: "border-box",
  };

  if (loadErr) {
    return (
      <div style={page}>
        <div style={cardStyle}>
          <h1 style={{ color: C.danger, fontSize: 18 }}>⚠ Invite unavailable</h1>
          <p style={{ color: C.dim }}>{loadErr}</p>
        </div>
      </div>
    );
  }

  if (done) {
    return (
      <div style={page}>
        <div style={cardStyle}>
          <h1 style={{ color: C.accent, fontSize: 20 }}>
            {done.running ? "⚡ Strategy is live" : "✓ Strategy received"}
          </h1>
          <p style={{ color: C.text, lineHeight: 1.6 }}>
            Thanks, {partnerName || "partner"}. Your strategy{" "}
            <code style={{ color: C.accent }}>{done.slug}</code>{" "}
            {done.running ? (
              <>
                is now running on the demo account and will start taking trades.
                You&apos;ll have read-only audit access to its live performance.
              </>
            ) : (
              <>
                was submitted and is pending review. You&apos;ll be granted
                read-only audit access once it&apos;s approved.
              </>
            )}
          </p>
          <a
            href="/partner"
            style={{
              display: "inline-block",
              marginTop: 20,
              padding: "12px 20px",
              background: C.accent,
              color: C.bg,
              borderRadius: 4,
              fontFamily: mono,
              fontWeight: 700,
              fontSize: 14,
              textDecoration: "none",
            }}
          >
            View your dashboard →
          </a>
          <p style={{ color: C.dim, fontSize: 12, marginTop: 10 }}>
            Sign in with{" "}
            <code style={{ color: C.text }}>{partnerEmail || "your email"}</code>{" "}
            to watch its live performance, fills, and slippage.
          </p>
        </div>
      </div>
    );
  }

  return (
    <div style={page}>
      <div style={cardStyle}>
        <div style={{ color: C.accent, fontSize: 13, letterSpacing: 2 }}>
          TRADE COPILOT · PARTNER STRATEGY UPLOAD
        </div>
        <h1 style={{ fontSize: 22, margin: "8px 0 4px" }}>
          {info?.label || "Submit your strategy"}
        </h1>
        <p style={{ color: C.dim, fontSize: 13, lineHeight: 1.6 }}>
          The platform handles the bar feed, broker execution, server-side
          stops, kill switch, and audit. You provide the signal logic — either
          upload a <code>.py</code> module or point us at your HTTPS endpoint.
        </p>

        {info?.instant_start && (
          <div
            style={{
              marginTop: 12,
              padding: 12,
              border: `1px solid ${C.accent}`,
              borderRadius: 4,
              color: C.accent,
              fontSize: 13,
              lineHeight: 1.5,
            }}
          >
            ⚡ This will start <strong>instantly</strong> on the demo account
            {info.account_label ? ` "${info.account_label}"` : ""} when you
            submit — no approval needed. Watch it take trades right away.
          </div>
        )}

        <label style={label}>Your name</label>
        <input style={input} value={partnerName} onChange={(e) => setPartnerName(e.target.value)} />

        <label style={label}>Your email</label>
        <input
          style={input}
          type="email"
          value={partnerEmail}
          onChange={(e) => setPartnerEmail(e.target.value)}
          placeholder="you@example.com"
        />

        <label style={label}>Strategy name</label>
        <input
          style={input}
          value={strategyName}
          onChange={(e) => setStrategyName(e.target.value)}
          placeholder="Velocity Spike"
        />

        <div style={{ display: "flex", gap: 16, marginTop: 16 }}>
          <div style={{ flex: 1 }}>
            <label style={label}>Instruments</label>
            <input style={input} value={instruments} onChange={(e) => setInstruments(e.target.value)} />
          </div>
          <div style={{ width: 120 }}>
            <label style={label}>Timeframe</label>
            <input style={input} value={timeframe} onChange={(e) => setTimeframe(e.target.value)} />
          </div>
        </div>

        <label style={label}>Delivery method</label>
        <div style={{ display: "flex", gap: 10 }}>
          {(["source", "http"] as const).map((d) => (
            <button
              key={d}
              type="button"
              onClick={() => setDelivery(d)}
              style={{
                flex: 1,
                padding: "12px",
                background: delivery === d ? C.accent : C.bg,
                color: delivery === d ? C.bg : C.text,
                border: `1px solid ${delivery === d ? C.accent : C.border}`,
                borderRadius: 4,
                cursor: "pointer",
                fontFamily: mono,
                fontWeight: 600,
              }}
            >
              {d === "source" ? "Upload .py file" : "HTTPS endpoint"}
            </button>
          ))}
        </div>

        {delivery === "source" ? (
          <>
            <label style={label}>Strategy module (.py)</label>
            <input
              style={{ ...input, padding: 8 }}
              type="file"
              accept=".py,text/x-python"
              onChange={(e) => setFile(e.target.files?.[0] || null)}
            />
            <p style={{ color: C.dim, fontSize: 12, marginTop: 6 }}>
              One <code>Strategy</code> subclass with <code>on_bar()</code> and a
              class-level <code>name</code>. Scanned for safety before storage.
            </p>
          </>
        ) : (
          <>
            <label style={label}>Endpoint URL (https)</label>
            <input
              style={input}
              value={endpointUrl}
              onChange={(e) => setEndpointUrl(e.target.value)}
              placeholder="https://your-server.example.com/signal"
            />
            <label style={label}>Shared HMAC secret</label>
            <input
              style={input}
              type="password"
              value={endpointSecret}
              onChange={(e) => setEndpointSecret(e.target.value)}
              placeholder="used to sign requests we send you"
            />
          </>
        )}

        <label style={label}>Params (JSON, optional)</label>
        <textarea
          style={{ ...input, minHeight: 64, resize: "vertical" }}
          value={paramsJson}
          onChange={(e) => setParamsJson(e.target.value)}
          placeholder='{"lookback": 20, "threshold": 1.5}'
        />

        <label style={label}>Backtest notes (optional)</label>
        <textarea
          style={{ ...input, minHeight: 64, resize: "vertical" }}
          value={backtest}
          onChange={(e) => setBacktest(e.target.value)}
        />

        {errorMsg && (
          <div
            style={{
              marginTop: 16,
              padding: 12,
              border: `1px solid ${C.danger}`,
              borderRadius: 4,
              color: C.danger,
            }}
          >
            {errorMsg}
            {findings && findings.length > 0 && (
              <ul style={{ margin: "8px 0 0", paddingLeft: 18 }}>
                {findings.map((f, i) => (
                  <li key={i} style={{ color: C.warn, fontSize: 13 }}>
                    [{f.code}] {f.message}
                    {f.line ? ` (line ${f.line})` : ""}
                  </li>
                ))}
              </ul>
            )}
          </div>
        )}

        <button
          type="button"
          onClick={submit}
          disabled={busy}
          style={{
            marginTop: 24,
            width: "100%",
            padding: 14,
            background: busy ? C.border : C.accent,
            color: C.bg,
            border: "none",
            borderRadius: 4,
            fontFamily: mono,
            fontWeight: 700,
            fontSize: 15,
            cursor: busy ? "default" : "pointer",
          }}
        >
          {busy ? "Submitting…" : "Submit strategy →"}
        </button>
      </div>
    </div>
  );
}
