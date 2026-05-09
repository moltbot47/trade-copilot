"use client";

import type { EquityPoint } from "@/lib/types";

type Props = {
  points: EquityPoint[];
  width?: number;
  height?: number;
};

export default function EquityCurve({
  points,
  width = 1000,
  height = 220,
}: Props) {
  if (!points || points.length === 0) {
    return (
      <div className="dim" style={{ fontSize: "0.85rem", padding: "1rem 0" }}>
        no closed trades yet — equity curve will populate after first close
      </div>
    );
  }

  const padding = { left: 40, right: 12, top: 16, bottom: 22 };
  const innerW = width - padding.left - padding.right;
  const innerH = height - padding.top - padding.bottom;

  const values = points.map((p) => p.cumulative_r);
  const min = Math.min(...values, 0);
  const max = Math.max(...values, 0);
  const range = max - min || 1;
  const stepX = points.length > 1 ? innerW / (points.length - 1) : innerW;

  const coords = values.map((v, i) => {
    const x = padding.left + i * stepX;
    const y = padding.top + innerH - ((v - min) / range) * innerH;
    return { x, y, v };
  });

  // Build segments split at zero crossings so we can color above/below
  const zeroY = padding.top + innerH - ((0 - min) / range) * innerH;

  const positivePts: string[] = [];
  const negativePts: string[] = [];
  coords.forEach((c) => {
    if (c.v >= 0) positivePts.push(`${c.x.toFixed(1)},${c.y.toFixed(1)}`);
    else negativePts.push(`${c.x.toFixed(1)},${c.y.toFixed(1)}`);
  });

  // Single full polyline colored by last value
  const fullPoints = coords
    .map((c) => `${c.x.toFixed(1)},${c.y.toFixed(1)}`)
    .join(" ");
  const lastV = values[values.length - 1];
  const stroke = lastV >= 0 ? "var(--accent)" : "var(--danger)";

  // Area fill below line (clip to zero baseline color)
  const areaPath =
    `M ${padding.left},${zeroY} ` +
    coords.map((c) => `L ${c.x.toFixed(1)},${c.y.toFixed(1)}`).join(" ") +
    ` L ${(padding.left + (coords.length - 1) * stepX).toFixed(1)},${zeroY} Z`;

  // Y-axis gridlines (3)
  const gridYs = [
    padding.top,
    padding.top + innerH / 2,
    padding.top + innerH,
  ];

  return (
    <div style={{ width: "100%", overflowX: "auto" }}>
      <svg
        width={width}
        height={height}
        viewBox={`0 0 ${width} ${height}`}
        style={{ width: "100%", height, display: "block" }}
        role="img"
        aria-label="Equity curve cumulative R"
      >
        {/* gridlines */}
        {gridYs.map((y, i) => (
          <line
            key={i}
            x1={padding.left}
            x2={width - padding.right}
            y1={y}
            y2={y}
            stroke="var(--border)"
            strokeDasharray="2 4"
          />
        ))}

        {/* zero baseline */}
        <line
          x1={padding.left}
          x2={width - padding.right}
          y1={zeroY}
          y2={zeroY}
          stroke="var(--text-dim)"
          strokeOpacity={0.5}
          strokeDasharray="4 4"
        />

        {/* fill */}
        <path d={areaPath} fill={stroke} fillOpacity={0.08} />

        {/* main line */}
        <polyline
          points={fullPoints}
          fill="none"
          stroke={stroke}
          strokeWidth={1.8}
        />

        {/* y labels */}
        <text
          x={4}
          y={padding.top + 10}
          fontSize="10"
          fill="var(--text-dim)"
          fontFamily="JetBrains Mono, monospace"
        >
          {max.toFixed(1)}R
        </text>
        <text
          x={4}
          y={zeroY + 4}
          fontSize="10"
          fill="var(--text-dim)"
          fontFamily="JetBrains Mono, monospace"
        >
          0R
        </text>
        <text
          x={4}
          y={padding.top + innerH}
          fontSize="10"
          fill="var(--text-dim)"
          fontFamily="JetBrains Mono, monospace"
        >
          {min.toFixed(1)}R
        </text>
      </svg>
      <div
        style={{
          display: "flex",
          justifyContent: "space-between",
          fontSize: "0.78rem",
          padding: "0 0.5rem",
        }}
      >
        <span className="dim">trade #1</span>
        <span style={{ color: stroke }}>
          cumulative {lastV.toFixed(2)}R
        </span>
        <span className="dim">trade #{points.length}</span>
      </div>
    </div>
  );
}
