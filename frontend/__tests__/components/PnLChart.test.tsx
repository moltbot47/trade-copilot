import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import PnLChart from "@/components/PnLChart";

describe("PnLChart", () => {
  it("renders empty state for null", () => {
    // @ts-expect-error - testing defensive null handling
    render(<PnLChart data={null} />);
    expect(screen.getByText(/no pnl data yet/i)).toBeInTheDocument();
  });

  it("renders empty state for empty array", () => {
    render(<PnLChart data={[]} />);
    expect(screen.getByText(/no pnl data yet/i)).toBeInTheDocument();
  });

  it("renders empty state for empty {points: []} shape", () => {
    render(<PnLChart data={{ points: [] }} />);
    expect(screen.getByText(/no pnl data yet/i)).toBeInTheDocument();
  });

  it("renders empty state for malformed object", () => {
    // @ts-expect-error - testing defensive unknown handling
    render(<PnLChart data={{ unexpected: 42 }} />);
    expect(screen.getByText(/no pnl data yet/i)).toBeInTheDocument();
  });

  it("renders empty state when all values are non-finite", () => {
    render(
      <PnLChart
        data={[
          { time: "t1", pnl: NaN },
          { time: "t2", pnl: Infinity },
        ]}
      />,
    );
    expect(screen.getByText(/no pnl data yet/i)).toBeInTheDocument();
  });

  it("renders sparkline for valid data", () => {
    render(
      <PnLChart
        data={[
          { time: "t1", pnl: 100 },
          { time: "t2", pnl: 150 },
          { time: "t3", pnl: 75 },
        ]}
      />,
    );
    // Sparkline adds an SVG with role=img
    expect(screen.getByRole("img", { name: /pnl sparkline/i })).toBeInTheDocument();
    // min/max/last labels
    expect(screen.getByText(/min/i)).toBeInTheDocument();
    expect(screen.getByText(/last/i)).toBeInTheDocument();
    expect(screen.getByText(/max/i)).toBeInTheDocument();
  });
});
