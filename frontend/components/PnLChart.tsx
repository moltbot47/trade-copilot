"use client";

import type { PnLPoint } from "@/lib/types";

type Props = {
  data: PnLPoint[] | { points?: PnLPoint[] } | unknown;
  width?: number;
  height?: number;
};

export default function PnLChart({ data, width = 600, height = 120 }: Props) {
  // Be defensive — backend may return [], {points: []}, null, or an error obj
  const series: PnLPoint[] = Array.isArray(data)
    ? (data as PnLPoint[])
    : Array.isArray((data as { points?: PnLPoint[] })?.points)
      ? (data as { points: PnLPoint[] }).points
      : [];

  if (series.length === 0) {
    return (
      <div className="dim" style={{ fontSize: "0.85rem", padding: "0.5rem 0" }}>
        no pnl data yet
      </div>
    );
  }

  const values = series
    .map((p) => Number(p?.pnl))
    .filter((v) => Number.isFinite(v));
  if (values.length === 0) {
    return (
      <div className="dim" style={{ fontSize: "0.85rem", padding: "0.5rem 0" }}>
        no pnl data yet
      </div>
    );
  }
  const min = Math.min(...values, 0);
  const max = Math.max(...values, 0);
  const range = max - min || 1;
  const stepX = values.length > 1 ? width / (values.length - 1) : width;

  const points = values
    .map((v, i) => {
      const x = i * stepX;
      const y = height - ((v - min) / range) * height;
      return `${x.toFixed(1)},${y.toFixed(1)}`;
    })
    .join(" ");

  const last = values[values.length - 1];
  const stroke = last >= 0 ? "var(--accent)" : "var(--danger)";

  // Zero line
  const zeroY =
    min < 0 && max > 0
      ? height - ((0 - min) / range) * height
      : null;

  return (
    <div style={{ width: "100%", overflowX: "auto" }}>
      <svg
        width={width}
        height={height}
        viewBox={`0 0 ${width} ${height}`}
        style={{ width: "100%", height, display: "block" }}
        role="img"
        aria-label="PnL sparkline"
      >
        {zeroY !== null && (
          <line
            x1={0}
            x2={width}
            y1={zeroY}
            y2={zeroY}
            stroke="var(--border)"
            strokeDasharray="4 4"
          />
        )}
        <polyline
          points={points}
          fill="none"
          stroke={stroke}
          strokeWidth={1.5}
        />
      </svg>
      <div style={{ display: "flex", justifyContent: "space-between", fontSize: "0.78rem" }}>
        <span className="dim">min ${min.toFixed(2)}</span>
        <span style={{ color: last >= 0 ? "var(--accent)" : "var(--danger)" }}>
          last ${last.toFixed(2)}
        </span>
        <span className="dim">max ${max.toFixed(2)}</span>
      </div>
    </div>
  );
}
