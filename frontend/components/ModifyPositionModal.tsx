"use client";

import { useEffect, useState } from "react";
import { api, ApiError } from "@/lib/api";

type Position = {
  id: string;
  symbol: string;
  side: string;
  qty: number;
  avg_price: number;
  unrealized_pl: number;
  has_sl: boolean;
  has_tp: boolean;
};

type Props = {
  position: Position;
  onClose: () => void;
  onSaved: () => void;
};

/**
 * Modal for setting / changing the broker-side SL and TP on an open
 * position. Shipped because of the 2026-05-11 incident where 3 orphan
 * positions sat unprotected on live accounts after the runner crashed
 * mid-flow. This lets the operator (or future user) attach safety
 * levels in one click without leaving Trade Copilot.
 *
 * Inputs are loosely validated client-side; the broker is the source
 * of truth (it'll reject too-close-to-current-price etc.) and any
 * validation error is rendered inline.
 */
export default function ModifyPositionModal({ position, onClose, onSaved }: Props) {
  // Pre-fill with a sensible default if the level isn't already set.
  // For SL: 0.5% adverse from entry. For TP: 0.8% favorable from entry.
  // The user can override before submitting.
  const isBuy = (position.side || "").toLowerCase() === "buy";
  const defaultSL = isBuy
    ? position.avg_price * (1 - 0.005)
    : position.avg_price * (1 + 0.005);
  const defaultTP = isBuy
    ? position.avg_price * (1 + 0.008)
    : position.avg_price * (1 - 0.008);

  const [sl, setSl] = useState<string>(defaultSL.toFixed(4));
  const [tp, setTp] = useState<string>(defaultTP.toFixed(4));
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [okMsg, setOkMsg] = useState<string | null>(null);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [onClose]);

  const submit = async () => {
    setErr(null);
    setOkMsg(null);
    setBusy(true);
    try {
      const slNum = sl.trim() === "" ? undefined : Number(sl);
      const tpNum = tp.trim() === "" ? undefined : Number(tp);
      if (slNum === undefined && tpNum === undefined) {
        setErr("Set at least one of stop loss or take profit.");
        setBusy(false);
        return;
      }
      if (slNum !== undefined && !Number.isFinite(slNum)) {
        setErr("Stop loss must be a number.");
        setBusy(false);
        return;
      }
      if (tpNum !== undefined && !Number.isFinite(tpNum)) {
        setErr("Take profit must be a number.");
        setBusy(false);
        return;
      }
      await api.modifyPositionLevels(position.id, {
        stop_loss: slNum,
        take_profit: tpNum,
      });
      setOkMsg("Saved — broker confirmed SL/TP attached.");
      onSaved();
      // Brief pause so the user can read the success message before close
      setTimeout(onClose, 800);
    } catch (e) {
      if (e instanceof ApiError) {
        setErr(e.message);
      } else {
        setErr((e as Error)?.message ?? "save failed");
      }
      setBusy(false);
    }
  };

  return (
    <div
      role="dialog"
      aria-modal="true"
      aria-labelledby="modify-pos-title"
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
            marginBottom: "0.5rem",
          }}
        >
          <h2 id="modify-pos-title" style={{ margin: 0 }} className="accent">
            Edit SL / TP · {position.symbol}
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

        <div
          style={{
            fontSize: "0.85rem",
            display: "flex",
            flexDirection: "column",
            gap: "0.25rem",
            marginBottom: "0.85rem",
          }}
        >
          <div>
            <span className="dim">side: </span>
            <span style={{ color: isBuy ? "var(--accent)" : "var(--danger)", fontWeight: 700 }}>
              {position.side?.toUpperCase()}
            </span>
            {"   "}
            <span className="dim">qty: </span>
            <span>{position.qty}</span>
            {"   "}
            <span className="dim">entry: </span>
            <span className="accent">{position.avg_price}</span>
          </div>
          <div>
            <span className="dim">current SL: </span>
            <span style={{ color: position.has_sl ? "var(--accent)" : "var(--danger)", fontWeight: 700 }}>
              {position.has_sl ? "set" : "🔴 NOT SET"}
            </span>
            {"   "}
            <span className="dim">current TP: </span>
            <span style={{ color: position.has_tp ? "var(--accent)" : "var(--danger)", fontWeight: 700 }}>
              {position.has_tp ? "set" : "🔴 NOT SET"}
            </span>
            {"   "}
            <span className="dim">P&L: </span>
            <span
              style={{
                color: position.unrealized_pl >= 0 ? "var(--accent)" : "var(--danger)",
              }}
            >
              ${position.unrealized_pl.toFixed(2)}
            </span>
          </div>
        </div>

        <div style={{ display: "flex", flexDirection: "column", gap: "0.6rem" }}>
          <label style={{ fontSize: "0.85rem" }}>
            stop loss (price)
            <input
              type="number"
              step="any"
              value={sl}
              onChange={(e) => setSl(e.target.value)}
              disabled={busy}
              style={{
                display: "block",
                width: "100%",
                padding: "0.4rem 0.6rem",
                marginTop: "0.25rem",
                fontFamily: "monospace",
              }}
            />
            <span className="dim" style={{ fontSize: "0.72rem" }}>
              leave empty to skip · default = 0.5% adverse from entry
            </span>
          </label>

          <label style={{ fontSize: "0.85rem" }}>
            take profit (price)
            <input
              type="number"
              step="any"
              value={tp}
              onChange={(e) => setTp(e.target.value)}
              disabled={busy}
              style={{
                display: "block",
                width: "100%",
                padding: "0.4rem 0.6rem",
                marginTop: "0.25rem",
                fontFamily: "monospace",
              }}
            />
            <span className="dim" style={{ fontSize: "0.72rem" }}>
              leave empty to skip · default = 0.8% favorable from entry
            </span>
          </label>
        </div>

        {err && (
          <p role="alert" className="danger" style={{ fontSize: "0.85rem", marginTop: "0.5rem" }}>
            {err}
          </p>
        )}
        {okMsg && (
          <p role="status" className="accent" style={{ fontSize: "0.85rem", marginTop: "0.5rem" }}>
            {okMsg}
          </p>
        )}

        <div style={{ display: "flex", gap: "0.5rem", justifyContent: "flex-end", marginTop: "1rem" }}>
          <button
            type="button"
            onClick={onClose}
            className="btn"
            disabled={busy}
            style={{ padding: "0.5rem 1rem" }}
          >
            cancel
          </button>
          <button
            type="button"
            onClick={submit}
            disabled={busy}
            className="btn btn-primary"
            style={{ padding: "0.5rem 1.1rem", fontWeight: 700 }}
          >
            {busy ? "saving…" : "save SL/TP"}
          </button>
        </div>
      </div>
    </div>
  );
}
