import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import StrategyStatusBadge from "@/components/StrategyStatusBadge";
import type { StrategyState } from "@/lib/types";

const baseState: StrategyState = {
  bot_id: 1,
  timeframe: "1m",
  is_running: true,
  confidence_threshold: 1.5,
  max_concurrent: 3,
  paused_until: null,
  last_signal_at: null,
  last_error: null,
};

describe("StrategyStatusBadge", () => {
  it("renders RUNNING when is_running and not paused", () => {
    render(<StrategyStatusBadge state={baseState} />);
    expect(screen.getByText("RUNNING")).toBeInTheDocument();
  });

  it("renders STOPPED when not running and not paused", () => {
    render(
      <StrategyStatusBadge state={{ ...baseState, is_running: false }} />,
    );
    expect(screen.getByText("STOPPED")).toBeInTheDocument();
  });

  it("renders PAUSED when paused_until is in the future", () => {
    const future = new Date(Date.now() + 30_000).toISOString();
    render(
      <StrategyStatusBadge
        state={{ ...baseState, is_running: true, paused_until: future }}
      />,
    );
    expect(screen.getByText("PAUSED")).toBeInTheDocument();
    expect(screen.getByText(/auto-resume/i)).toBeInTheDocument();
  });

  it("ignores paused_until in the past (renders RUNNING)", () => {
    const past = new Date(Date.now() - 30_000).toISOString();
    render(
      <StrategyStatusBadge
        state={{ ...baseState, is_running: true, paused_until: past }}
      />,
    );
    expect(screen.getByText("RUNNING")).toBeInTheDocument();
  });

  it("renders 'unknown' for null state", () => {
    render(<StrategyStatusBadge state={null} />);
    expect(screen.getByText(/unknown/i)).toBeInTheDocument();
  });
});
