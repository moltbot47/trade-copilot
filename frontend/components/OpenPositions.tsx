"use client";

import { useCallback, useEffect, useState } from "react";
import { api } from "@/lib/api";
import ModifyPositionModal from "./ModifyPositionModal";

type Position = {
  id: string;
  symbol: string;
  side: string;
  qty: number;
  avg_price: number;
  unrealized_pl: number;
  has_sl: boolean;
  has_tp: boolean;
  stop_loss_id: string | null;
  take_profit_id: string | null;
};

/**
 * Live broker positions with one-click SL/TP edit. Highlights positions
 * that are unprotected (no SL or no TP) in red — that's a real risk
 * surface, especially after the 2026-05-11 incident where 3 orphan
 * positions sat live without exit levels.
 */
export default function OpenPositions() {
  const [positions, setPositions] = useState<Position[] | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [editing, setEditing] = useState<Position | null>(null);

  const refresh = useCallback(async () => {
    setErr(null);
    try {
      const data = await api.listOpenPositions();
      setPositions(data.positions);
    } catch (e) {
      setErr((e as Error)?.message ?? "could not load positions");
    }
  }, []);

  useEffect(() => {
    refresh();
    const t = setInterval(refresh, 30_000); // 30s — broker poll is light
    return () => clearInterval(t);
  }, [refresh]);

  const unprotectedCount = (positions || []).filter(
    (p) => !p.has_sl || !p.has_tp,
  ).length;

  return (
    <section className="card" aria-labelledby="positions-heading">
      <header
        style={{
          display: "flex",
          justifyContent: "space-between",
          alignItems: "baseline",
          marginBottom: "0.5rem",
        }}
      >
        <h2 id="positions-heading" style={{ margin: 0 }} className="accent">
          {">"} open positions
        </h2>
        <div style={{ fontSize: "0.78rem" }} className="dim">
          {positions === null
            ? "loading…"
            : positions.length === 0
              ? "none"
              : `${positions.length} open${unprotectedCount > 0 ? ` · ${unprotectedCount} unprotected ⚠` : ""}`}
        </div>
      </header>

      {err && (
        <p className="danger" style={{ fontSize: "0.85rem" }}>
          {err}
        </p>
      )}

      {positions && unprotectedCount > 0 && (
        <div
          role="alert"
          style={{
            padding: "0.55rem 0.75rem",
            marginBottom: "0.5rem",
            border: "1px solid var(--danger)",
            background: "rgba(255, 60, 60, 0.06)",
            fontSize: "0.85rem",
          }}
        >
          <strong style={{ color: "var(--danger)" }}>
            ⚠ {unprotectedCount} position{unprotectedCount === 1 ? "" : "s"} without
            full SL + TP coverage.
          </strong>{" "}
          <span className="dim">
            Click <em>edit</em> below to attach safety levels — broker-side, so
            they trigger even if Trade Copilot is offline.
          </span>
        </div>
      )}

      {positions && positions.length > 0 && (
        <div style={{ overflowX: "auto" }}>
          <table
            style={{
              width: "100%",
              borderCollapse: "collapse",
              fontSize: "0.85rem",
              fontFamily: "monospace",
            }}
          >
            <thead>
              <tr style={{ borderBottom: "1px solid var(--accent-dim)" }}>
                {[
                  "SYMBOL",
                  "SIDE",
                  "QTY",
                  "ENTRY",
                  "P&L",
                  "SL",
                  "TP",
                  "",
                ].map((h) => (
                  <th
                    key={h}
                    className="dim"
                    style={{ textAlign: "left", padding: "0.35rem 0.5rem", fontWeight: 400 }}
                  >
                    {h}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {positions.map((p) => {
                const isBuy = (p.side || "").toLowerCase() === "buy";
                const profit = p.unrealized_pl >= 0;
                return (
                  <tr
                    key={p.id}
                    style={{
                      borderBottom: "1px solid rgba(255,255,255,0.04)",
                    }}
                  >
                    <td style={{ padding: "0.4rem 0.5rem" }}>
                      <span className="accent" style={{ fontWeight: 700 }}>
                        {p.symbol}
                      </span>
                    </td>
                    <td
                      style={{
                        padding: "0.4rem 0.5rem",
                        color: isBuy ? "var(--accent)" : "var(--danger)",
                        fontWeight: 700,
                      }}
                    >
                      {(p.side || "").toUpperCase()}
                    </td>
                    <td style={{ padding: "0.4rem 0.5rem" }}>{p.qty}</td>
                    <td style={{ padding: "0.4rem 0.5rem" }}>{p.avg_price}</td>
                    <td
                      style={{
                        padding: "0.4rem 0.5rem",
                        color: profit ? "var(--accent)" : "var(--danger)",
                      }}
                    >
                      ${p.unrealized_pl.toFixed(2)}
                    </td>
                    <td
                      style={{
                        padding: "0.4rem 0.5rem",
                        color: p.has_sl ? "var(--accent)" : "var(--danger)",
                        fontWeight: p.has_sl ? 400 : 700,
                      }}
                    >
                      {p.has_sl ? "set" : "🔴 NONE"}
                    </td>
                    <td
                      style={{
                        padding: "0.4rem 0.5rem",
                        color: p.has_tp ? "var(--accent)" : "var(--danger)",
                        fontWeight: p.has_tp ? 400 : 700,
                      }}
                    >
                      {p.has_tp ? "set" : "🔴 NONE"}
                    </td>
                    <td style={{ padding: "0.4rem 0.5rem", textAlign: "right" }}>
                      <button
                        type="button"
                        onClick={() => setEditing(p)}
                        className="btn"
                        style={{
                          padding: "0.25rem 0.6rem",
                          fontSize: "0.78rem",
                          cursor: "pointer",
                        }}
                      >
                        edit SL/TP
                      </button>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}

      {editing && (
        <ModifyPositionModal
          position={editing}
          onClose={() => setEditing(null)}
          onSaved={() => {
            // refresh after save so the SL/TP badge flips to green
            refresh();
          }}
        />
      )}
    </section>
  );
}
