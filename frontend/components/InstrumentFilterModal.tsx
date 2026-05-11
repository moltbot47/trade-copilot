"use client";

import { useEffect, useMemo, useState } from "react";
import { api } from "@/lib/api";
import type { Bot, Subscription } from "@/lib/types";

/**
 * Lightweight modal for editing an existing subscription's
 * `allowed_instruments`. The PATCH endpoint already supports this — we
 * just needed the UI. Mirrors the SubscribeModal checkbox layout so
 * users have one mental model whether they're picking on subscribe or
 * editing afterward.
 */
type Props = {
  sub: Subscription;
  bot: Bot | undefined;
  onClose: () => void;
  onSaved: (updated: Subscription) => void;
};

function botInstruments(bot: Bot | undefined): string[] {
  if (!bot) return [];
  const raw = bot.instruments_csv ?? "";
  const seen: string[] = [];
  for (const token of raw.split(",")) {
    const sym = token.trim().toUpperCase();
    if (sym && !seen.includes(sym)) seen.push(sym);
  }
  return seen;
}

export default function InstrumentFilterModal({ sub, bot, onClose, onSaved }: Props) {
  const all = useMemo(() => botInstruments(bot), [bot]);

  // None / empty in the subscription = "all instruments." Pre-check
  // accordingly so users see the current state at a glance.
  const initialSelected = useMemo(() => {
    if (!sub.allowed_instruments || sub.allowed_instruments.length === 0) {
      return new Set(all);
    }
    return new Set(sub.allowed_instruments.map((s) => s.toUpperCase()));
  }, [sub.allowed_instruments, all]);

  const [selected, setSelected] = useState<Set<string>>(initialSelected);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  const allChecked = selected.size === all.length && all.length > 0;
  const noneChecked = selected.size === 0;

  const toggle = (sym: string) => {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(sym)) next.delete(sym);
      else next.add(sym);
      return next;
    });
  };

  // ESC closes
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [onClose]);

  const save = async () => {
    setErr(null);
    setBusy(true);
    try {
      // Same semantics as SubscribeModal: all-selected → send null
      // ("inherit all"). Anything else → send the list.
      const payload = allChecked ? null : Array.from(selected);
      const updated = await api.updateSubscription(sub.id, {
        allowed_instruments: payload,
      });
      onSaved(updated);
    } catch (e) {
      setErr((e as Error)?.message ?? "save failed");
      setBusy(false);
    }
  };

  return (
    <div
      role="dialog"
      aria-modal="true"
      aria-labelledby="filter-modal-title"
      onClick={onClose}
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
    >
      <div
        className="card"
        onClick={(e) => e.stopPropagation()}
        style={{ width: "100%", maxWidth: 460, padding: "1.25rem" }}
      >
        <header
          style={{
            display: "flex",
            justifyContent: "space-between",
            alignItems: "center",
            marginBottom: "0.75rem",
          }}
        >
          <h2 id="filter-modal-title" style={{ margin: 0 }} className="accent">
            Edit instruments
          </h2>
          <button
            type="button"
            onClick={onClose}
            aria-label="Close"
            className="dim"
            style={{
              background: "transparent",
              border: "none",
              fontSize: "1.2rem",
              cursor: "pointer",
            }}
          >
            ×
          </button>
        </header>

        <p className="dim" style={{ fontSize: "0.85rem", marginTop: 0 }}>
          Choose which of the bot's instruments to receive signals for.
          Unchecking a row stops new signals on that pair; open positions
          are unaffected and continue under existing management rules.
        </p>

        <div
          style={{
            display: "flex",
            justifyContent: "space-between",
            alignItems: "baseline",
            margin: "0.5rem 0 0.4rem",
          }}
        >
          <span className="dim" style={{ fontSize: "0.85rem" }}>
            {bot ? bot.name : `Bot #${sub.bot_id}`}
          </span>
          <button
            type="button"
            onClick={() => setSelected(allChecked ? new Set() : new Set(all))}
            className="dim"
            style={{
              background: "transparent",
              border: "none",
              fontSize: "0.78rem",
              textDecoration: "underline",
              cursor: "pointer",
              padding: 0,
            }}
          >
            {allChecked ? "deselect all" : "select all"}
          </button>
        </div>

        <div
          role="group"
          aria-label="bot instruments"
          style={{
            display: "flex",
            flexWrap: "wrap",
            gap: "0.4rem 0.75rem",
            fontSize: "0.85rem",
            marginBottom: "1rem",
          }}
        >
          {all.length === 0 && (
            <span className="dim">No instruments declared on this bot.</span>
          )}
          {all.map((sym) => {
            const checked = selected.has(sym);
            return (
              <label
                key={sym}
                style={{
                  display: "inline-flex",
                  alignItems: "center",
                  gap: "0.35rem",
                  cursor: "pointer",
                  userSelect: "none",
                }}
              >
                <input
                  type="checkbox"
                  checked={checked}
                  onChange={() => toggle(sym)}
                  aria-label={sym}
                />
                <span className={checked ? "accent" : "dim"}>{sym}</span>
              </label>
            );
          })}
        </div>

        {noneChecked && (
          <p
            className="danger"
            style={{ fontSize: "0.78rem", marginTop: "0.2rem", marginBottom: "0.6rem" }}
          >
            Pick at least one instrument, or you'll receive no signals from this bot.
          </p>
        )}

        {err && (
          <p className="danger" style={{ fontSize: "0.85rem" }}>
            {err}
          </p>
        )}

        <div style={{ display: "flex", gap: "0.5rem", justifyContent: "flex-end" }}>
          <button
            type="button"
            onClick={onClose}
            className="btn"
            style={{ padding: "0.5rem 1rem" }}
          >
            cancel
          </button>
          <button
            type="button"
            onClick={save}
            disabled={busy || noneChecked || all.length === 0}
            className="btn btn-primary"
            style={{ padding: "0.5rem 1.1rem", fontWeight: 700 }}
          >
            {busy ? "saving…" : "save"}
          </button>
        </div>
      </div>
    </div>
  );
}
