"use client";

import { useEffect, useRef, useState } from "react";

type Props = {
  label: string;
  value: string;
  trend?: "up" | "down" | "flat" | null;
  color?: string;
  subtitle?: string;
};

/**
 * StatsCard — pulses briefly when the displayed value changes. Pulse color
 * follows the trend (up=accent green, down=danger red, flat=text-dim).
 *
 * The pulse is a short box-shadow flash + value-text scale-up via the
 * `pulse` class (defined in globals.css). 320ms total. Doesn't trigger on
 * initial mount — only on actual changes after the first render.
 */
export default function StatsCard({
  label,
  value,
  trend,
  color,
  subtitle,
}: Props) {
  const arrow =
    trend === "up" ? "▲" : trend === "down" ? "▼" : trend === "flat" ? "→" : "";
  const arrowColor =
    trend === "up"
      ? "var(--accent)"
      : trend === "down"
      ? "var(--danger)"
      : "var(--text-dim)";

  // Pulse on value change (skip first render)
  const prevValue = useRef(value);
  const firstRender = useRef(true);
  const [pulseKey, setPulseKey] = useState(0);

  useEffect(() => {
    if (firstRender.current) {
      firstRender.current = false;
      return;
    }
    if (prevValue.current !== value) {
      prevValue.current = value;
      setPulseKey((k) => k + 1);
    }
  }, [value]);

  const pulseColor =
    trend === "up"
      ? "var(--accent)"
      : trend === "down"
      ? "var(--danger)"
      : "var(--text-dim)";

  return (
    <div
      key={pulseKey}
      className="card stats-card-pulse"
      style={
        {
          background: "var(--bg)",
          padding: "1rem",
          display: "flex",
          flexDirection: "column",
          gap: "0.35rem",
          ["--pulse-color" as string]: pulseColor,
        } as React.CSSProperties
      }
    >
      <div
        className="dim"
        style={{
          fontSize: "0.72rem",
          textTransform: "uppercase",
          letterSpacing: "0.08em",
        }}
      >
        {label}
      </div>
      <div
        style={{
          color: color || "var(--text)",
          fontSize: "1.5rem",
          fontWeight: 700,
          display: "flex",
          alignItems: "baseline",
          gap: "0.4rem",
        }}
      >
        <span>{value}</span>
        {arrow && (
          <span style={{ color: arrowColor, fontSize: "0.85rem" }}>
            {arrow}
          </span>
        )}
      </div>
      {subtitle && (
        <div className="dim" style={{ fontSize: "0.72rem" }}>
          {subtitle}
        </div>
      )}
    </div>
  );
}
