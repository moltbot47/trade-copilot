"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { api } from "@/lib/api";
import EmailGate from "@/components/EmailGate";

const WEBHOOK_PATTERN =
  /^https:\/\/discord\.com\/api\/webhooks\/\d+\/[A-Za-z0-9_-]+$/;

export default function NotificationsSettings() {
  const [currentStatus, setCurrentStatus] = useState<{
    has_webhook: boolean;
    masked: string | null;
  } | null>(null);
  const [url, setUrl] = useState("");
  const [saving, setSaving] = useState(false);
  const [testing, setTesting] = useState(false);
  const [msg, setMsg] = useState<{ kind: "ok" | "err"; text: string } | null>(null);

  const refresh = async () => {
    try {
      setCurrentStatus(await api.getDiscordWebhook());
    } catch (err) {
      setMsg({ kind: "err", text: (err as Error).message });
    }
  };

  useEffect(() => {
    refresh();
  }, []);

  const valid = !url || WEBHOOK_PATTERN.test(url);

  const save = async () => {
    setSaving(true);
    setMsg(null);
    try {
      const r = await api.setDiscordWebhook(url || null);
      setCurrentStatus(r);
      setUrl("");
      setMsg({ kind: "ok", text: r.has_webhook ? "Webhook saved." : "Webhook cleared." });
    } catch (err) {
      setMsg({ kind: "err", text: (err as Error).message });
    } finally {
      setSaving(false);
    }
  };

  const sendTest = async () => {
    setTesting(true);
    setMsg(null);
    try {
      await api.testDiscordWebhook();
      setMsg({
        kind: "ok",
        text: "Test message sent — check your Discord channel.",
      });
    } catch (err) {
      setMsg({ kind: "err", text: (err as Error).message });
    } finally {
      setTesting(false);
    }
  };

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: "1.25rem" }}>
      <EmailGate />
      <header>
        <div className="dim" style={{ fontSize: "0.85rem" }}>
          <Link href="/settings" style={{ color: "var(--text-dim)" }}>
            ‹ settings
          </Link>
          {" > "}notifications
        </div>
        <h1 style={{ margin: "0.25rem 0" }}>
          <span className="accent">Discord notifications</span>
        </h1>
        <p className="dim" style={{ margin: 0, fontSize: "0.88rem", maxWidth: "60ch" }}>
          The bot posts every entry, partial close, exit, and confidence-bearing
          scan to your channel. Each user can configure their own webhook so
          your signals don't mix with anyone else's.
        </p>
      </header>

      <section className="card" style={{ padding: "1.25rem" }}>
        <h2 style={{ marginTop: 0 }} className="accent">
          {">"} current status
        </h2>
        {currentStatus === null ? (
          <p className="dim">loading…</p>
        ) : currentStatus.has_webhook ? (
          <div>
            <span className="accent">configured</span>{" "}
            <span className="dim">({currentStatus.masked})</span>
            <div style={{ marginTop: "0.75rem", display: "flex", gap: "0.5rem", flexWrap: "wrap" }}>
              <button
                onClick={sendTest}
                disabled={testing}
                className="btn"
                style={{ padding: "0.55rem 1rem" }}
              >
                {testing ? "sending…" : "send test message"}
              </button>
              <button
                onClick={async () => {
                  if (!confirm("Remove your Discord webhook?")) return;
                  setUrl("");
                  await save();
                  refresh();
                }}
                className="btn"
                style={{
                  padding: "0.55rem 1rem",
                  borderColor: "var(--danger)",
                  color: "var(--danger)",
                }}
              >
                disconnect
              </button>
            </div>
          </div>
        ) : (
          <p className="dim">no webhook configured — paste one below</p>
        )}
      </section>

      <section className="card" style={{ padding: "1.25rem" }}>
        <h2 style={{ marginTop: 0 }} className="accent">
          {">"} {currentStatus?.has_webhook ? "replace webhook" : "add webhook"}
        </h2>
        <ol style={{ paddingLeft: "1.2rem", marginTop: 0, fontSize: "0.9rem" }}>
          <li>In Discord, open the channel you want signals in.</li>
          <li>
            Channel settings → Integrations → Webhooks → <b>New Webhook</b>.
          </li>
          <li>Name it "Trade Copilot" and click <b>Copy Webhook URL</b>.</li>
          <li>Paste it below and click Save.</li>
        </ol>
        <div style={{ marginTop: "0.75rem" }}>
          <input
            type="url"
            value={url}
            onChange={(e) => setUrl(e.target.value)}
            placeholder="https://discord.com/api/webhooks/<id>/<token>"
            style={{
              width: "100%",
              padding: "0.7rem",
              fontFamily: "inherit",
              background: "var(--bg)",
              color: "var(--text)",
              border: `1px solid ${
                url && !valid ? "var(--danger)" : "var(--border)"
              }`,
            }}
          />
          {url && !valid && (
            <div className="danger" style={{ fontSize: "0.78rem", marginTop: "0.3rem" }}>
              must look like https://discord.com/api/webhooks/&lt;id&gt;/&lt;token&gt;
            </div>
          )}
          <button
            onClick={save}
            disabled={saving || !valid || !url}
            className="btn"
            style={{ marginTop: "0.75rem", padding: "0.6rem 1.2rem" }}
          >
            {saving ? "saving…" : "save"}
          </button>
        </div>
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
