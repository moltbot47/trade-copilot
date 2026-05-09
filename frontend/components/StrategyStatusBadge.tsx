"use client";

import type { StrategyState } from "@/lib/types";

type Props = {
  state: StrategyState | null;
};

function formatRelativeTime(iso: string): string {
  const target = new Date(iso).getTime();
  const now = Date.now();
  const diffMs = target - now;
  if (Number.isNaN(target)) return iso;
  const abs = Math.abs(diffMs);
  if (abs < 60_000) return `${Math.round(diffMs / 1000)}s`;
  if (abs < 3_600_000) return `${Math.round(diffMs / 60_000)}m`;
  return new Intl.DateTimeFormat(undefined, {
    hour: "2-digit",
    minute: "2-digit",
  }).format(new Date(iso));
}

export default function StrategyStatusBadge({ state }: Props) {
  if (!state) {
    return (
      <span
        className="dim"
        style={{
          padding: "0.3rem 0.7rem",
          border: "1px solid var(--border)",
          fontSize: "0.78rem",
          textTransform: "uppercase",
          letterSpacing: "0.08em",
        }}
      >
        unknown
      </span>
    );
  }

  const isPaused =
    state.paused_until && new Date(state.paused_until).getTime() > Date.now();

  let color = "var(--danger)";
  let label = "STOPPED";
  let extra: string | null = null;

  if (isPaused) {
    color = "var(--warn)";
    label = "PAUSED";
    extra = state.paused_until
      ? `auto-resume in ${formatRelativeTime(state.paused_until)}`
      : null;
  } else if (state.is_running) {
    color = "var(--accent)";
    label = "RUNNING";
  }

  return (
    <span
      style={{
        display: "inline-flex",
        alignItems: "center",
        gap: "0.5rem",
      }}
    >
      <span
        style={{
          padding: "0.3rem 0.7rem",
          border: `1px solid ${color}`,
          color,
          fontSize: "0.78rem",
          fontWeight: 700,
          textTransform: "uppercase",
          letterSpacing: "0.08em",
        }}
      >
        {label}
      </span>
      {extra && (
        <span className="dim" style={{ fontSize: "0.8rem" }}>
          {extra}
        </span>
      )}
    </span>
  );
}
