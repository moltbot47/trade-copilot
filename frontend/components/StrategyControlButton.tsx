"use client";

import { useEffect, useState } from "react";

type Props = {
  isRunning: boolean;
  onStart: () => Promise<void>;
  onStop: () => Promise<void>;
  disabled?: boolean;
};

/**
 * StrategyControlButton — optimistic, snappy.
 *
 * Click → instantly flip the visual to "starting…" / "stopping…" and show a
 * spinner. Don't wait for the server before updating state. If the server
 * call eventually fails the parent's onStart/onStop will throw and the
 * button reverts. If it succeeds, the parent updates `isRunning` via the
 * WS strategy event and our optimistic state collapses.
 *
 * After 5 seconds without a confirming `isRunning` flip, we show "still
 * <verb>…" so the user knows we're waiting on the server, not stuck.
 */
export default function StrategyControlButton({
  isRunning,
  onStart,
  onStop,
  disabled,
}: Props) {
  const [busy, setBusy] = useState(false);
  const [pendingTarget, setPendingTarget] = useState<boolean | null>(null);
  const [stillWaiting, setStillWaiting] = useState(false);

  // Once parent state matches our optimistic target, clear pending.
  useEffect(() => {
    if (pendingTarget !== null && isRunning === pendingTarget) {
      setPendingTarget(null);
      setStillWaiting(false);
      setBusy(false);
    }
  }, [isRunning, pendingTarget]);

  // After 5s of waiting, show "still …" hint
  useEffect(() => {
    if (pendingTarget === null) return;
    const t = setTimeout(() => setStillWaiting(true), 5000);
    return () => clearTimeout(t);
  }, [pendingTarget]);

  const handleClick = async () => {
    if (busy) return;
    const target = !isRunning;
    setPendingTarget(target);
    setBusy(true);
    try {
      if (isRunning) await onStop();
      else await onStart();
      // Don't clear busy here — let the WS confirmation do it. If the
      // server returned synchronously (REST fallback), parent will have
      // already updated isRunning and the useEffect above will clear.
    } catch (err) {
      // On error, revert optimistic state immediately
      setPendingTarget(null);
      setStillWaiting(false);
      setBusy(false);
      throw err;
    }
  };

  // Visual = optimistic if pending, else real
  const visualRunning = pendingTarget ?? isRunning;
  const showSpinner = busy && pendingTarget !== null;

  let label: string;
  if (busy) {
    const verb = pendingTarget ? "starting" : "stopping";
    label = stillWaiting ? `still ${verb}…` : `${verb}…`;
  } else {
    label = visualRunning ? "stop" : "start";
  }

  return (
    <button
      onClick={handleClick}
      disabled={busy || disabled}
      className="btn"
      style={{
        padding: "0.7rem 1.6rem",
        fontSize: "0.95rem",
        fontWeight: 700,
        borderColor: visualRunning ? "var(--danger)" : "var(--accent-dim)",
        color: visualRunning ? "var(--danger)" : "var(--accent)",
        background: "transparent",
        opacity: busy ? 0.85 : 1,
        transition: "border-color 180ms ease, color 180ms ease",
      }}
    >
      {showSpinner && <span className="spinner-inline" />}
      [{label}]
    </button>
  );
}
