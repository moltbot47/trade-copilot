"use client";

type Props = {
  label: string;
  value: string;
  trend?: "up" | "down" | "flat" | null;
  color?: string;
  subtitle?: string;
};

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

  return (
    <div
      className="card"
      style={{
        background: "var(--bg)",
        padding: "1rem",
        display: "flex",
        flexDirection: "column",
        gap: "0.35rem",
      }}
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
