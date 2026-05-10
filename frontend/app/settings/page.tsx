"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { api } from "@/lib/api";
import EmailGate from "@/components/EmailGate";

export default function SettingsHome() {
  const [mfaEnabled, setMfaEnabled] = useState<boolean | null>(null);
  const [webhookSet, setWebhookSet] = useState<boolean | null>(null);

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
    })();
  }, []);

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
