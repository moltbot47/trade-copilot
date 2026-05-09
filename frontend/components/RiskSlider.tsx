"use client";

import { useEffect, useRef, useState } from "react";

type Props = {
  value: number;
  onChange: (v: number) => void;
  disabled?: boolean;
};

function label(v: number): string {
  if (v <= 2) return "Conservative";
  if (v <= 4) return "Cautious";
  if (v <= 6) return "Balanced";
  if (v <= 8) return "Bold";
  return "Aggressive";
}

export default function RiskSlider({ value, onChange, disabled }: Props) {
  const [local, setLocal] = useState(value);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => setLocal(value), [value]);

  const handle = (v: number) => {
    setLocal(v);
    if (timer.current) clearTimeout(timer.current);
    timer.current = setTimeout(() => {
      onChange(v);
    }, 500);
  };

  const lbl = label(local);
  const color =
    local <= 4 ? "var(--accent)" : local <= 7 ? "var(--warn)" : "var(--danger)";

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: "0.4rem" }}>
      <div style={{ display: "flex", justifyContent: "space-between", fontSize: "0.85rem" }}>
        <span className="dim">aggression</span>
        <span style={{ color }}>
          [{local}] {lbl}
        </span>
      </div>
      <input
        type="range"
        min={1}
        max={10}
        step={1}
        value={local}
        disabled={disabled}
        aria-label="Aggression level"
        onChange={(e) => handle(parseInt(e.target.value, 10))}
      />
      <div style={{ display: "flex", justifyContent: "space-between", fontSize: "0.7rem", color: "var(--text-dim)" }}>
        <span>1 cons</span>
        <span>5 bal</span>
        <span>10 aggr</span>
      </div>
    </div>
  );
}
