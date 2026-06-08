"use client";

import { useEffect, useState } from "react";
import { api, ApiError } from "@/lib/api";
import type { Bot } from "@/lib/types";

type Props = {
  bot: Bot;
  onClose: () => void;
  onSuccess: () => void;
};

// Aggression-to-multiplier mirror of backend's risk_engine._aggression_multiplier
function aggressionMultiplier(level: number): number {
  const a = Math.max(1, Math.min(10, level));
  if (a <= 5) return 0.25 + (a - 1) * (0.75 / 4);
  return 1.0 + (a - 5) * (1.0 / 5);
}

// Parse Bot.instruments_csv → list of canonical symbols (uppercased, deduped,
// in declared order). Used to render the per-bot instrument checkboxes.
function botInstruments(bot: Bot): string[] {
  const raw = bot.instruments_csv ?? "";
  const seen: string[] = [];
  for (const token of raw.split(",")) {
    const sym = token.trim().toUpperCase();
    if (sym && !seen.includes(sym)) seen.push(sym);
  }
  return seen;
}

export default function SubscribeModal({ bot, onClose, onSuccess }: Props) {
  const [aggression, setAggression] = useState(5);
  const [balance, setBalance] = useState<number | null>(null);
  const [connected, setConnected] = useState<boolean | null>(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  // Per-user instrument filter. Default: all of the bot's instruments selected
  // (= same behavior as before this feature shipped). Deselecting some
  // narrows the user to a subset; deselecting all is blocked by the submit
  // button's disabled state below.
  const instruments = botInstruments(bot);
  const [selectedInstruments, setSelectedInstruments] = useState<Set<string>>(
    () => new Set(instruments),
  );
  const toggleInstrument = (sym: string) => {
    setSelectedInstruments((prev) => {
      const next = new Set(prev);
      if (next.has(sym)) next.delete(sym);
      else next.add(sym);
      return next;
    });
  };
  const allSelected = instruments.length > 0 && selectedInstruments.size === instruments.length;
  const noneSelected = selectedInstruments.size === 0;

  useEffect(() => {
    (async () => {
      try {
        const acc = await api.getAccountState();
        setBalance(acc.balance ?? null);
        setConnected(!!acc.connected);
      } catch {
        setConnected(false);
      }
    })();
  }, []);

  // ESC closes
  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    document.addEventListener("keydown", handler);
    return () => document.removeEventListener("keydown", handler);
  }, [onClose]);

  // Rough per-trade risk estimate. The backend uses lot × ATR for true risk;
  // here we approximate using broker-level info we have client-side:
  //   lot = 0.01 (forced for crypto on tiny accounts) × multiplier
  //   risk ≈ lot × 1ATR × instrument_multiplier
  // We don't have live ATR client-side, so we estimate via "typical bar size."
  // Order of magnitude only — this is a SAFETY preview, not a guarantee.
  const baseLot = 0.01;
  const lot = baseLot * aggressionMultiplier(aggression);
  // Heuristic risk per trade: assume ~1% adverse move on the typical $80k BTC
  // = $800 notional × lot. Worst case before SL kicks in.
  const estRiskUsd = balance ? Math.min(lot * 800 * 0.01 * 5, balance * 0.05) : null;
  const riskPctOfBalance = balance && estRiskUsd ? (estRiskUsd / balance) * 100 : null;

  const submit = async () => {
    setErr(null);
    setBusy(true);
    try {
      // If the user kept every instrument selected, send null — that maps to
      // "all instruments" in the backend and avoids a stale snapshot if the
      // bot's instrument list is expanded later.
      const instrumentsPayload = allSelected ? null : Array.from(selectedInstruments);
      await api.subscribeToBot(bot.id, aggression, instrumentsPayload);
      onSuccess();
    } catch (e) {
      if (e instanceof ApiError && e.status === 409) {
        // already subscribed — that's success-equivalent
        onSuccess();
        return;
      }
      setErr((e as Error)?.message || "subscribe failed");
      setBusy(false);
    }
  };

  const blocked = connected === false;

  return (
    <div
      role="dialog"
      aria-modal="true"
      aria-labelledby="sub-modal-title"
      style={{
        position: "fixed",
        inset: 0,
        zIndex: 1000,
        background: "rgba(0, 0, 0, 0.7)",
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        padding: "1rem",
      }}
      onClick={onClose}
    >
      <div
        className="card"
        onClick={(e) => e.stopPropagation()}
        style={{
          maxWidth: 520,
          width: "100%",
          padding: "1.5rem",
          borderColor: "var(--accent-dim)",
        }}
      >
        <header
          style={{
            display: "flex",
            justifyContent: "space-between",
            alignItems: "center",
            marginBottom: "0.75rem",
          }}
        >
          <h2 id="sub-modal-title" style={{ margin: 0 }} className="accent">
            Subscribe to {bot.name}
          </h2>
          <button
            onClick={onClose}
            className="dim"
            aria-label="Close"
            style={{
              background: "transparent",
              border: "none",
              color: "var(--text-dim)",
              fontSize: "1.5rem",
              cursor: "pointer",
              padding: 0,
              lineHeight: 1,
            }}
          >
            ×
          </button>
        </header>

        {blocked && (
          <div
            role="alert"
            style={{
              padding: "0.85rem 1rem",
              border: "1px solid var(--danger)",
              background: "rgba(211, 47, 47, 0.07)",
              marginBottom: "1rem",
              fontSize: "0.88rem",
            }}
          >
            <span className="danger" style={{ fontWeight: 700 }}>
              ⚠ No broker connected.
            </span>{" "}
            You must connect a TradeLocker account first.{" "}
            <a href="/connect" style={{ color: "var(--accent)" }}>
              Connect now →
            </a>
          </div>
        )}

        <p style={{ fontSize: "0.92rem", marginTop: 0 }}>
          {bot.description}
        </p>

        {/* Per-user instrument filter — all checked by default = legacy behavior. */}
        {instruments.length > 0 && (
          <div style={{ margin: "1rem 0" }} aria-labelledby="instruments-label">
            <div
              id="instruments-label"
              style={{
                fontSize: "0.88rem",
                marginBottom: "0.45rem",
                display: "flex",
                justifyContent: "space-between",
                alignItems: "baseline",
              }}
            >
              <span className="dim">Instruments to trade:</span>
              <button
                type="button"
                onClick={() =>
                  setSelectedInstruments(
                    allSelected ? new Set() : new Set(instruments),
                  )
                }
                className="dim"
                style={{
                  background: "transparent",
                  border: "none",
                  fontSize: "0.72rem",
                  textDecoration: "underline",
                  cursor: "pointer",
                  padding: 0,
                }}
              >
                {allSelected ? "deselect all" : "select all"}
              </button>
            </div>
            <div
              role="group"
              aria-label="bot instruments"
              style={{
                display: "grid",
                gridTemplateColumns: "repeat(auto-fit, minmax(110px, 1fr))",
                gap: "0.5rem 0.75rem",
                fontSize: "0.85rem",
              }}
            >
              {instruments.map((sym) => {
                const checked = selectedInstruments.has(sym);
                return (
                  <label
                    key={sym}
                    style={{
                      display: "flex",
                      alignItems: "center",
                      gap: "0.5rem",
                      cursor: "pointer",
                      userSelect: "none",
                      minWidth: 0,
                    }}
                  >
                    <input
                      type="checkbox"
                      checked={checked}
                      onChange={() => toggleInstrument(sym)}
                      aria-label={sym}
                      style={{ flexShrink: 0, width: "1rem", height: "1rem" }}
                    />
                    <span
                      className={checked ? "accent" : "dim"}
                      style={{
                        overflow: "hidden",
                        textOverflow: "ellipsis",
                        whiteSpace: "nowrap",
                      }}
                    >
                      {sym}
                    </span>
                  </label>
                );
              })}
            </div>
            {noneSelected && (
              <p
                className="danger"
                style={{ fontSize: "0.78rem", marginTop: "0.4rem", marginBottom: 0 }}
              >
                Pick at least one instrument to subscribe.
              </p>
            )}
          </div>
        )}

        <div style={{ margin: "1rem 0" }}>
          <label
            htmlFor="agg-slider"
            style={{ fontSize: "0.88rem", display: "block", marginBottom: "0.5rem" }}
          >
            <span className="dim">Aggression:</span>{" "}
            <span className="accent" style={{ fontWeight: 700 }}>
              {aggression} / 10
            </span>{" "}
            <span className="dim" style={{ fontSize: "0.78rem" }}>
              (multiplier: {aggressionMultiplier(aggression).toFixed(2)}×)
            </span>
          </label>
          <input
            id="agg-slider"
            type="range"
            min={1}
            max={10}
            value={aggression}
            onChange={(e) => setAggression(parseInt(e.target.value, 10))}
            style={{ width: "100%" }}
          />
          <div
            style={{
              display: "flex",
              justifyContent: "space-between",
              fontSize: "0.72rem",
            }}
            className="dim"
          >
            <span>1 (conservative · 0.25×)</span>
            <span>5 (balanced · 1.00×)</span>
            <span>10 (aggressive · 2.00×)</span>
          </div>
        </div>

        {/* Risk preview */}
        <div
          className="card"
          style={{
            padding: "0.85rem 1rem",
            background: "var(--bg)",
            margin: "0.5rem 0 1rem",
            fontSize: "0.85rem",
          }}
        >
          <div className="dim" style={{ marginBottom: "0.4rem" }}>
            risk preview (approximate)
          </div>
          <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: "0.5rem" }}>
            <div>
              <span className="dim">your balance:</span>{" "}
              <span className="accent">
                {balance !== null ? `$${balance.toFixed(2)}` : "—"}
              </span>
            </div>
            <div>
              <span className="dim">lot per trade:</span>{" "}
              <span className="accent">{lot.toFixed(2)}</span>
            </div>
            <div>
              <span className="dim">est. risk:</span>{" "}
              <span className="accent">
                {estRiskUsd !== null ? `~$${estRiskUsd.toFixed(2)}` : "—"}
              </span>
            </div>
            <div>
              <span className="dim">% of balance:</span>{" "}
              <span
                style={{
                  color:
                    riskPctOfBalance && riskPctOfBalance > 5
                      ? "var(--danger)"
                      : "var(--accent)",
                }}
              >
                {riskPctOfBalance !== null ? `~${riskPctOfBalance.toFixed(1)}%` : "—"}
              </span>
            </div>
          </div>
          <p className="dim" style={{ marginTop: "0.5rem", marginBottom: 0, fontSize: "0.72rem" }}>
            Estimate only. The bot enforces lot caps + a daily kill switch
            and {">"}1.5 ATR drawdown auto-exit.
          </p>
        </div>

        {err && (
          <p className="danger" style={{ fontSize: "0.85rem" }}>
            {err}
          </p>
        )}

        <div style={{ display: "flex", gap: "0.5rem", justifyContent: "flex-end" }}>
          <button onClick={onClose} className="btn" style={{ padding: "0.6rem 1rem" }}>
            cancel
          </button>
          <button
            onClick={submit}
            disabled={busy || blocked || noneSelected}
            className="btn btn-primary"
            style={{ padding: "0.6rem 1.2rem", fontWeight: 700 }}
          >
            {busy
              ? "subscribing…"
              : blocked
                ? "connect broker first"
                : noneSelected
                  ? "pick instruments"
                  : "Confirm subscribe"}
          </button>
        </div>
      </div>
    </div>
  );
}
