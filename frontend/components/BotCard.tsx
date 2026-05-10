"use client";

import Link from "next/link";
import { useState } from "react";
import { getUserEmail } from "@/lib/api";
import type { Bot } from "@/lib/types";
import SubscribeModal from "./SubscribeModal";

function RiskBars({ level }: { level: number }) {
  const cells = [1, 2, 3, 4, 5];
  let cls: "on" | "warn" | "danger" = "on";
  if (level >= 4) cls = "danger";
  else if (level >= 3) cls = "warn";
  return (
    <div className="bar-track" aria-label={`Risk level ${level} of 5`}>
      {cells.map((c) => (
        <div
          key={c}
          className={`bar-cell ${c <= level ? cls : ""}`}
        />
      ))}
    </div>
  );
}

export default function BotCard({ bot }: { bot: Bot }) {
  const winRate = bot.backtest_win_rate ?? 0;
  const profitFactor = bot.backtest_profit_factor ?? 0;
  const instruments = (bot.instruments_csv ?? "")
    .split(",")
    .map((s) => s.trim())
    .filter(Boolean);
  const riskLevel = bot.risk_level ?? 3;

  return (
    <article className="card" style={{ display: "flex", flexDirection: "column", gap: "0.7rem" }}>
      <header style={{ display: "flex", alignItems: "baseline", justifyContent: "space-between", gap: "0.5rem" }}>
        <h3 style={{ margin: 0, color: "var(--accent)" }}>{bot.name}</h3>
        <span className="dim" style={{ fontSize: "0.78rem" }}>
          {bot.strategy_type || ""}
        </span>
      </header>
      <p style={{ margin: 0, color: "var(--text)", fontSize: "0.92rem", minHeight: "3em" }}>
        {bot.description}
      </p>
      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: "0.5rem", fontSize: "0.85rem" }}>
        <div>
          <div className="dim">Win rate</div>
          <div className="accent">{winRate.toFixed(1)}%</div>
        </div>
        <div>
          <div className="dim">Profit factor</div>
          <div className="accent">{profitFactor.toFixed(2)}</div>
        </div>
        <div>
          <div className="dim">Risk</div>
          <RiskBars level={riskLevel} />
        </div>
        <div>
          <div className="dim">Instruments</div>
          <div>{instruments.join(", ") || "—"}</div>
        </div>
      </div>
      <SubscribeButton bot={bot} />
    </article>
  );
}

function SubscribeButton({ bot }: { bot: Bot }) {
  const [open, setOpen] = useState(false);

  const onClick = () => {
    if (!getUserEmail()) {
      // Not logged in — send to strategy page; EmailGate prompts there.
      window.location.href = `/strategy?bot=${encodeURIComponent(bot.slug)}`;
      return;
    }
    setOpen(true);
  };

  return (
    <div>
      <button
        type="button"
        className="btn btn-primary"
        onClick={onClick}
      >
        Subscribe
      </button>
      {/* Quiet escape hatch for users who want to connect Genesis FX first. */}
      <Link
        href={`/connect?bot=${encodeURIComponent(bot.slug)}`}
        className="dim"
        style={{
          fontSize: "0.72rem",
          textDecoration: "underline",
          marginLeft: "0.6rem",
        }}
      >
        connect broker first
      </Link>
      {open && (
        <SubscribeModal
          bot={bot}
          onClose={() => setOpen(false)}
          onSuccess={() => {
            window.location.href = `/strategy?bot=${encodeURIComponent(bot.slug)}`;
          }}
        />
      )}
    </div>
  );
}
