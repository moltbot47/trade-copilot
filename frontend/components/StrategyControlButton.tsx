"use client";

import { useState } from "react";

type Props = {
  isRunning: boolean;
  onStart: () => Promise<void>;
  onStop: () => Promise<void>;
  disabled?: boolean;
};

export default function StrategyControlButton({
  isRunning,
  onStart,
  onStop,
  disabled,
}: Props) {
  const [busy, setBusy] = useState(false);

  const handleClick = async () => {
    setBusy(true);
    try {
      if (isRunning) await onStop();
      else await onStart();
    } finally {
      setBusy(false);
    }
  };

  const label = busy
    ? isRunning
      ? "stopping..."
      : "starting..."
    : isRunning
    ? "stop"
    : "start";

  return (
    <button
      onClick={handleClick}
      disabled={busy || disabled}
      className="btn"
      style={{
        padding: "0.7rem 1.6rem",
        fontSize: "0.95rem",
        fontWeight: 700,
        borderColor: isRunning ? "var(--danger)" : "var(--accent-dim)",
        color: isRunning ? "var(--danger)" : "var(--accent)",
        background: "transparent",
      }}
    >
      [{label}]
    </button>
  );
}
