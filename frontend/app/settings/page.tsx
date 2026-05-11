"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { api } from "@/lib/api";
import EmailGate from "@/components/EmailGate";
import type { RiskAppetite } from "@/lib/types";

const APPETITE_OPTIONS: { value: RiskAppetite; label: string; description: string }[] = [
  {
    value: "conservative",
    label: "conservative",
    description: "≥1.5σ entries · 2:1 R:R · 30% margin cap per pair",
  },
  {
    value: "balanced",
    label: "balanced",
    description: "≥0.8σ entries · 1.5:1 R:R · 50% margin cap per pair",
  },
  {
    value: "aggressive",
    label: "aggressive",
    description: "≥0.3σ entries · 1:1 R:R · 75% margin cap · tiny-account compounding",
  },
];

export default function SettingsHome() {
  const [mfaEnabled, setMfaEnabled] = useState<boolean | null>(null);
  const [webhookSet, setWebhookSet] = useState<boolean | null>(null);
  const [appetite, setAppetite] = useState<RiskAppetite | null>(null);
  const [savingAppetite, setSavingAppetite] = useState(false);
  const [appetiteErr, setAppetiteErr] = useState<string | null>(null);

  useEffect(() => {
    (async () => {
      try {
        const m = await api.getMfaStatus();
        setMfaEnabled(m.enabled);
      } catch {
        setMfaEnabled(null);
      }
      try {
        const w = await api.getDiscordWebhook();
        setWebhookSet(w.has_webhook);
      } catch {
        setWebhookSet(null);
      }
      try {
        const me = await api.getMe();
        setAppetite(me.risk_appetite);
      } catch {
        setAppetite(null);
      }
    })();
  }, []);

  const saveAppetite = async (next: RiskAppetite) => {
    if (next === appetite) return;
    const previous = appetite;
    setAppetite(next); // optimistic
    setAppetiteErr(null);
    setSavingAppetite(true);
    try {
      await api.updateMe({ risk_appetite: next });
    } catch (e) {
      setAppetite(previous);
      setAppetiteErr((e as Error).message ?? "save failed");
    } finally {
      setSavingAppetite(false);
    }
  };

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: "1.25rem" }}>
      <EmailGate />
      <header>
        <div className="dim" style={{ fontSize: "0.85rem" }}>
          {">"} account & security
        </div>
        <h1 style={{ margin: "0.25rem 0" }}>
          <span className="accent">Settings</span>
        </h1>
      </header>

      {/* Risk appetite — inline (no nav). Drives the advisor's defaults +
          (once shipped) the LaT-PFN strategy threshold + TP curve. */}
      <section className="card" style={{ padding: "1.25rem" }}>
        <div style={{ fontSize: "1.1rem", fontWeight: 700, marginBottom: "0.25rem" }}>
          Risk appetite
        </div>
        <div className="dim" style={{ fontSize: "0.88rem", marginBottom: "0.85rem" }}>
          Drives the tiny-account advisor's recommendations and the LaT-PFN
          entry threshold. You can change it any time.
        </div>
        <div
          role="radiogroup"
          aria-label="Risk appetite"
          style={{
            display: "flex",
            gap: "0.5rem",
            flexWrap: "wrap",
            marginBottom: "0.6rem",
          }}
        >
          {APPETITE_OPTIONS.map((opt) => {
            const active = appetite === opt.value;
            return (
              <button
                key={opt.value}
                type="button"
                role="radio"
                aria-checked={active}
                disabled={savingAppetite || appetite === null}
                onClick={() => saveAppetite(opt.value)}
                className="btn"
                style={{
                  padding: "0.4rem 0.95rem",
                  fontSize: "0.88rem",
                  borderColor: active ? "var(--accent)" : "var(--accent-dim)",
                  color: active ? "var(--accent)" : "var(--dim)",
                  fontWeight: active ? 700 : 400,
                  cursor: savingAppetite ? "wait" : "pointer",
                }}
              >
                {opt.label}
              </button>
            );
          })}
        </div>
        {appetite && (
          <div className="dim" style={{ fontSize: "0.82rem" }}>
            {APPETITE_OPTIONS.find((o) => o.value === appetite)?.description}
          </div>
        )}
        {appetiteErr && (
          <div className="danger" style={{ fontSize: "0.82rem", marginTop: "0.4rem" }}>
            could not save: {appetiteErr}
          </div>
        )}
      </section>

      <SettingsCard
        title="Discord notifications"
        status={webhookSet === null ? "—" : webhookSet ? "configured" : "not set"}
        statusOk={!!webhookSet}
        href="/settings/notifications"
        body="Route the bot's trade signals + confidence scores to your own Discord channel. Each user can configure their own webhook."
      />

      <SettingsCard
        title="Two-factor authentication"
        status={mfaEnabled === null ? "—" : mfaEnabled ? "enabled" : "disabled"}
        statusOk={!!mfaEnabled}
        href="/settings/security"
        body="Add a 6-digit code from your authenticator app on every login. Critical if your bot trades real money — protects against email compromise."
      />

      <SettingsCard
        title="Audit log"
        status="view"
        statusOk
        href="/settings/audit-log"
        body="Every security-sensitive action taken on your account: logins, MFA changes, broker connections, panic-stops."
      />
    </div>
  );
}

function SettingsCard({
  title,
  status,
  statusOk,
  href,
  body,
}: {
  title: string;
  status: string;
  statusOk: boolean;
  href: string;
  body: string;
}) {
  return (
    <Link href={href} style={{ textDecoration: "none" }}>
      <section
        className="card"
        style={{
          padding: "1.25rem",
          display: "grid",
          gridTemplateColumns: "1fr auto",
          gap: "0.75rem",
          alignItems: "center",
          cursor: "pointer",
          borderColor: "var(--border)",
        }}
      >
        <div>
          <div style={{ fontSize: "1.1rem", fontWeight: 700, marginBottom: "0.25rem" }}>
            {title}
          </div>
          <div className="dim" style={{ fontSize: "0.88rem" }}>
            {body}
          </div>
        </div>
        <div
          className="dim"
          style={{
            color: statusOk ? "var(--accent)" : "var(--text-dim)",
            textTransform: "uppercase",
            fontSize: "0.78rem",
            fontWeight: 700,
            letterSpacing: "0.06em",
          }}
        >
          {status} ›
        </div>
      </section>
    </Link>
  );
}
