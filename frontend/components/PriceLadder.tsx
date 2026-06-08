"use client";

import { useMemo } from "react";

export interface LadderPosition {
  side: "buy" | "sell" | null;
  qty: number;
  avgPrice: number | null;
}

export interface PriceLadderProps {
  mid: number;
  tickSize: number;
  rows?: number;           // total rows; defaults to 21 (10 above + mid + 10 below)
  position?: LadderPosition;
  bestBid?: number | null;
  bestAsk?: number | null;
  onClickPrice?: (price: number, side: "buy" | "sell") => void;
}

/**
 * Vertical price ladder. Best ask above mid, best bid below mid.
 * Click on a price row in the BUY column (left) → place buy limit at that price.
 * Click in the SELL column (right) → place sell limit.
 * Position row highlighted at avgPrice.
 */
export default function PriceLadder({
  mid,
  tickSize,
  rows = 21,
  position,
  bestBid,
  bestAsk,
  onClickPrice,
}: PriceLadderProps) {
  const halfRows = Math.floor(rows / 2);

  const levels = useMemo(() => {
    const out: number[] = [];
    for (let i = halfRows; i >= -halfRows; i--) {
      out.push(roundToTick(mid + i * tickSize, tickSize));
    }
    return out;
  }, [mid, tickSize, halfRows]);

  const positionPrice = position?.avgPrice
    ? roundToTick(position.avgPrice, tickSize)
    : null;

  return (
    <div
      style={{
        border: "1px solid var(--border-strong)",
        background: "var(--bg-card)",
        userSelect: "none",
      }}
    >
      <div
        style={{
          display: "grid",
          gridTemplateColumns: "1fr 1.2fr 1fr",
          padding: "0.5rem 0.75rem",
          borderBottom: "1px solid var(--border)",
          fontSize: "0.7rem",
          letterSpacing: "0.1em",
          color: "var(--text-dim)",
          textTransform: "uppercase",
        }}
      >
        <span>buy</span>
        <span style={{ textAlign: "center" }}>price</span>
        <span style={{ textAlign: "right" }}>sell</span>
      </div>
      <div style={{ maxHeight: "60vh", overflowY: "auto" }}>
        {levels.map((price) => {
          const isAsk = bestAsk != null && price >= bestAsk && price < bestAsk + tickSize;
          const isBid = bestBid != null && price <= bestBid && price > bestBid - tickSize;
          const isMid = Math.abs(price - mid) < tickSize / 2;
          const isPosition =
            positionPrice != null && Math.abs(price - positionPrice) < tickSize / 2;

          const rowBg = isPosition
            ? "rgba(255, 170, 0, 0.10)"
            : isMid
            ? "rgba(0, 255, 65, 0.04)"
            : "transparent";

          return (
            <div
              key={price}
              style={{
                display: "grid",
                gridTemplateColumns: "1fr 1.2fr 1fr",
                fontSize: "0.85rem",
                background: rowBg,
                borderBottom: "1px solid rgba(42, 42, 42, 0.3)",
              }}
            >
              <button
                type="button"
                onClick={() => onClickPrice?.(price, "buy")}
                style={ladderCell({
                  align: "left",
                  highlight: isBid,
                  color: "var(--accent)",
                })}
                aria-label={`Buy limit at ${price}`}
              >
                {isBid ? "●" : ""}
              </button>
              <div
                style={{
                  ...ladderCell({ align: "center" }),
                  color: isMid
                    ? "var(--accent)"
                    : isPosition
                    ? "var(--warn)"
                    : "var(--text)",
                  fontWeight: isMid || isPosition ? 700 : 500,
                  cursor: "default",
                }}
              >
                {formatPrice(price, tickSize)}
                {isPosition && position && (
                  <span
                    style={{
                      marginLeft: "0.5rem",
                      fontSize: "0.7rem",
                      color: position.side === "buy" ? "var(--accent)" : "var(--danger)",
                    }}
                  >
                    {position.side?.toUpperCase()} {position.qty}
                  </span>
                )}
              </div>
              <button
                type="button"
                onClick={() => onClickPrice?.(price, "sell")}
                style={ladderCell({
                  align: "right",
                  highlight: isAsk,
                  color: "var(--danger)",
                })}
                aria-label={`Sell limit at ${price}`}
              >
                {isAsk ? "●" : ""}
              </button>
            </div>
          );
        })}
      </div>
    </div>
  );
}

function ladderCell(opts: { align: "left" | "center" | "right"; highlight?: boolean; color?: string }): React.CSSProperties {
  return {
    padding: "0.35rem 0.75rem",
    textAlign: opts.align,
    color: opts.color ?? "var(--text)",
    background: opts.highlight ? "rgba(0, 255, 65, 0.08)" : "transparent",
    border: "none",
    cursor: opts.align === "center" ? "default" : "pointer",
    fontFamily: "inherit",
    fontSize: "0.85rem",
    minHeight: 28,
    width: "100%",
  };
}

function roundToTick(v: number, tick: number): number {
  return Math.round(v / tick) * tick;
}

function formatPrice(v: number, tick: number): string {
  const decimals = Math.max(0, -Math.floor(Math.log10(tick)));
  return v.toFixed(decimals);
}
