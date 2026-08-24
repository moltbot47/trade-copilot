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

// Resolve which performance figures to display. Prefer live stats computed
// from real (demo) trades; fall back to seeded backtest figures; otherwise
// return null so the card shows an honest "collecting data" state instead of
// a misleading 0.0% / 0.00.
function resolveStats(bot: Bot): {
  source: "live" | "backtest" | "none";
  winRate: number | null;
  profitFactor: number | null;
  liveTrades: number;
} {
  const source =
    bot.stats_source ??
    ((bot.backtest_win_rate ?? 0) > 0 || (bot.backtest_profit_factor ?? 0) > 0
      ? "backtest"
      : "none");
  if (source === "live") {
    return {
      source,
      winRate: bot.live_win_rate ?? null,
      profitFactor: bot.live_profit_factor ?? null,
      liveTrades: bot.live_total_trades ?? 0,
    };
  }
  if (source === "backtest") {
    return {
      source,
      winRate: bot.backtest_win_rate ?? null,
      profitFactor: bot.backtest_profit_factor ?? null,
      liveTrades: 0,
    };
  }
  return { source: "none", winRate: null, profitFactor: null, liveTrades: 0 };
}

function StatsBadge({
  source,
  liveTrades,
}: {
  source: "live" | "backtest" | "none";
  liveTrades: number;
}) {
  if (source === "live") {
    return (
      <span
        className="accent"
        title={`Computed from ${liveTrades} real closed trade${liveTrades === 1 ? "" : "s"}`}
        style={{ fontSize: "0.7rem", fontWeight: 700, letterSpacing: "0.04em" }}
      >
        ● LIVE · {liveTrades} trade{liveTrades === 1 ? "" : "s"}
      </span>
    );
  }
  if (source === "backtest") {
    return (
      <span className="dim" title="Historical backtest figures — not live results" style={{ fontSize: "0.7rem" }}>
        backtest
      </span>
    );
  }
  return (
    <span className="dim" title="No trades recorded yet" style={{ fontSize: "0.7rem" }}>
      no live data yet
    </span>
  );
}

export default function BotCard({
  bot,
  brokerConnected,
}: {
  bot: Bot;
  brokerConnected?: boolean;
}) {
  const { source, winRate, profitFactor, liveTrades } = resolveStats(bot);
  const hasStats = winRate !== null && profitFactor !== null;
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
          <div className="dim" style={{ display: "flex", alignItems: "baseline", gap: "0.4rem", flexWrap: "wrap" }}>
            Win rate <StatsBadge source={source} liveTrades={liveTrades} />
          </div>
          <div className="accent">{hasStats ? `${winRate!.toFixed(1)}%` : "—"}</div>
        </div>
        <div>
          <div className="dim">Profit factor</div>
          <div className="accent">{hasStats ? profitFactor!.toFixed(2) : "—"}</div>
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
      {!hasStats && (
        <p className="dim" style={{ margin: 0, fontSize: "0.75rem" }}>
          Collecting live results — stats appear once this bot closes its first trades on a demo account.
        </p>
      )}
      <SubscribeButton bot={bot} brokerConnected={brokerConnected} />
    </article>
  );
}

function SubscribeButton({
  bot,
  brokerConnected,
}: {
  bot: Bot;
  brokerConnected?: boolean;
}) {
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
      {brokerConnected === false && (
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
      )}
      {brokerConnected === true && (
        <span
          className="dim"
          style={{ fontSize: "0.72rem", marginLeft: "0.6rem" }}
        >
          broker connected
        </span>
      )}
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
