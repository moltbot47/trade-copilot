"use client";

import { useState } from "react";

export interface OrderTicketProps {
  instrument: string;
  qty: number;
  onQtyChange: (q: number) => void;
  onMarketOrder: (side: "buy" | "sell", brackets: Brackets) => void;
  onFlatten: () => void;
  onReverse: () => void;
  busy?: boolean;
  lastResult?: string | null;
  // Optional gating — wired from the DOM page so desktop can't bypass the
  // role/cap checks that the mobile big-buttons already enforce.
  readOnly?: boolean;
  capQtyHit?: boolean;
  dailyLossHit?: boolean;
  blockReason?: string | null;
}

export interface Brackets {
  sl?: number;
  tp?: number;
}

export default function OrderTicket({
  instrument,
  qty,
  onQtyChange,
  onMarketOrder,
  onFlatten,
  onReverse,
  busy,
  lastResult,
  readOnly,
  capQtyHit,
  dailyLossHit,
  blockReason,
}: OrderTicketProps) {
  // Effective busy = the prop OR any role/cap block. Every button below
  // reads `effectiveBusy` so the desktop ticket honors the same gating as
  // the mobile buy/sell buttons. This closes the desktop-bypass hole the
  // review caught — without this, an employee on desktop could fire orders
  // that violated their cap/role.
  const blocked = !!readOnly || !!capQtyHit || !!dailyLossHit;
  const effectiveBusy = !!busy || blocked;
  const [sl, setSl] = useState<string>("");
  const [tp, setTp] = useState<string>("");

  const parsedSl = sl.trim() ? parseFloat(sl) : undefined;
  const parsedTp = tp.trim() ? parseFloat(tp) : undefined;
  const brackets: Brackets = {
    sl: Number.isFinite(parsedSl) ? parsedSl : undefined,
    tp: Number.isFinite(parsedTp) ? parsedTp : undefined,
  };

  return (
    <div
      style={{
        border: "1px solid var(--border-strong)",
        background: "var(--bg-card)",
        padding: "1rem",
        display: "flex",
        flexDirection: "column",
        gap: "0.85rem",
      }}
    >
      <div>
        <div className="dim" style={{ fontSize: "0.7rem", letterSpacing: "0.1em" }}>
          {">"} ticket
        </div>
        <div style={{ fontSize: "1.1rem", color: "var(--accent)", fontWeight: 700 }}>
          {instrument}
        </div>
      </div>

      {blocked && (
        <div
          style={{
            border: "1px solid var(--warn)",
            color: "var(--warn)",
            padding: "0.5rem 0.7rem",
            fontSize: "0.78rem",
            background: "rgba(255, 170, 0, 0.06)",
          }}
        >
          {readOnly && "Viewer access — order entry disabled. "}
          {capQtyHit && "Lot exceeds your per-order cap. "}
          {dailyLossHit && "Daily loss cap reached. "}
          {blockReason && <span>{blockReason}</span>}
        </div>
      )}

      <label style={{ display: "flex", flexDirection: "column", gap: "0.25rem" }}>
        <span className="dim" style={{ fontSize: "0.7rem", textTransform: "uppercase" }}>
          qty
        </span>
        <div style={{ display: "flex", alignItems: "center", gap: "0.4rem" }}>
          <button
            type="button"
            className="btn"
            style={{ flex: "0 0 38px", padding: "0.4rem" }}
            onClick={() => onQtyChange(Math.max(0.01, +(qty - 0.01).toFixed(2)))}
            aria-label="Decrease quantity"
          >
            −
          </button>
          <input
            type="number"
            step="0.01"
            min="0.01"
            value={qty}
            onChange={(e) => onQtyChange(parseFloat(e.target.value) || 0.01)}
            style={inputStyle}
          />
          <button
            type="button"
            className="btn"
            style={{ flex: "0 0 38px", padding: "0.4rem" }}
            onClick={() => onQtyChange(+(qty + 0.01).toFixed(2))}
            aria-label="Increase quantity"
          >
            +
          </button>
        </div>
      </label>

      <label style={{ display: "flex", flexDirection: "column", gap: "0.25rem" }}>
        <span className="dim" style={{ fontSize: "0.7rem", textTransform: "uppercase" }}>
          stop loss (price)
        </span>
        <input
          type="number"
          step="0.01"
          value={sl}
          placeholder="—"
          onChange={(e) => setSl(e.target.value)}
          style={inputStyle}
        />
      </label>

      <label style={{ display: "flex", flexDirection: "column", gap: "0.25rem" }}>
        <span className="dim" style={{ fontSize: "0.7rem", textTransform: "uppercase" }}>
          take profit (price)
        </span>
        <input
          type="number"
          step="0.01"
          value={tp}
          placeholder="—"
          onChange={(e) => setTp(e.target.value)}
          style={inputStyle}
        />
      </label>

      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: "0.5rem" }}>
        <button
          type="button"
          disabled={effectiveBusy}
          onClick={() => onMarketOrder("buy", brackets)}
          style={{
            ...bigBtn,
            background: "var(--accent)",
            color: "var(--bg)",
            borderColor: "var(--accent)",
          }}
          aria-label="Buy market (F1)"
        >
          BUY MKT <kbd style={kbdStyle}>F1</kbd>
        </button>
        <button
          type="button"
          disabled={effectiveBusy}
          onClick={() => onMarketOrder("sell", brackets)}
          style={{
            ...bigBtn,
            background: "var(--danger)",
            color: "var(--bg)",
            borderColor: "var(--danger)",
          }}
          aria-label="Sell market (F2)"
        >
          SELL MKT <kbd style={kbdStyle}>F2</kbd>
        </button>
      </div>

      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: "0.5rem" }}>
        <button
          type="button"
          disabled={effectiveBusy}
          onClick={onFlatten}
          className="btn"
          style={{ borderColor: "var(--warn)", color: "var(--warn)" }}
          aria-label="Flatten all (F3)"
        >
          FLATTEN <kbd style={kbdStyle}>F3</kbd>
        </button>
        <button
          type="button"
          disabled={effectiveBusy}
          onClick={onReverse}
          className="btn"
          aria-label="Reverse position (R)"
        >
          REVERSE <kbd style={kbdStyle}>R</kbd>
        </button>
      </div>

      {lastResult && (
        <div
          style={{
            fontSize: "0.78rem",
            color: lastResult.startsWith("OK") ? "var(--accent)" : "var(--danger)",
            background: "var(--bg)",
            border: "1px solid var(--border)",
            padding: "0.5rem 0.65rem",
            fontFamily: "inherit",
            whiteSpace: "pre-wrap",
            wordBreak: "break-word",
          }}
        >
          {lastResult}
        </div>
      )}
    </div>
  );
}

const inputStyle: React.CSSProperties = {
  background: "var(--bg)",
  color: "var(--text)",
  border: "1px solid var(--border)",
  padding: "0.5rem 0.6rem",
  fontFamily: "inherit",
  fontSize: "0.95rem",
  flex: 1,
  width: "100%",
};

const bigBtn: React.CSSProperties = {
  padding: "0.85rem 0.4rem",
  fontWeight: 700,
  fontSize: "0.95rem",
  letterSpacing: "0.08em",
  textTransform: "uppercase",
  border: "1px solid",
  cursor: "pointer",
  minHeight: 56,
  display: "flex",
  alignItems: "center",
  justifyContent: "center",
  gap: "0.5rem",
};

const kbdStyle: React.CSSProperties = {
  fontSize: "0.65rem",
  padding: "0.1rem 0.3rem",
  border: "1px solid rgba(0,0,0,0.25)",
  borderRadius: 2,
  background: "rgba(0,0,0,0.15)",
  fontFamily: "inherit",
};
