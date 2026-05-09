import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import BotCard from "@/components/BotCard";
import type { Bot } from "@/lib/types";

const fullBot: Bot = {
  id: 1,
  slug: "orb-breakout",
  name: "ORB Breakout",
  description: "Opening range breakout strategy",
  backtest_win_rate: 62,
  backtest_profit_factor: 1.4,
  risk_level: 3,
  instruments_csv: "EURUSD,GBPUSD",
  strategy_type: "orb",
  is_active: true,
};

describe("BotCard", () => {
  it("renders all backtest_* fields", () => {
    render(<BotCard bot={fullBot} />);
    expect(screen.getByText("ORB Breakout")).toBeInTheDocument();
    expect(screen.getByText(/Opening range breakout/i)).toBeInTheDocument();
    expect(screen.getByText("62.0%")).toBeInTheDocument();
    expect(screen.getByText("1.40")).toBeInTheDocument();
    expect(screen.getByText("EURUSD, GBPUSD")).toBeInTheDocument();
    expect(screen.getByText("orb")).toBeInTheDocument();
  });

  it("renders Subscribe link with bot slug", () => {
    render(<BotCard bot={fullBot} />);
    const link = screen.getByRole("link", { name: /subscribe/i });
    expect(link).toHaveAttribute(
      "href",
      "/connect?bot=orb-breakout",
    );
  });

  it("renders the risk-level bar with aria-label", () => {
    render(<BotCard bot={fullBot} />);
    expect(
      screen.getByLabelText(/risk level 3 of 5/i),
    ).toBeInTheDocument();
  });

  it("handles missing backtest_win_rate by defaulting to 0", () => {
    const partial: Bot = { ...fullBot, backtest_win_rate: undefined };
    render(<BotCard bot={partial} />);
    expect(screen.getByText("0.0%")).toBeInTheDocument();
  });

  it("handles missing instruments_csv gracefully", () => {
    const partial: Bot = { ...fullBot, instruments_csv: undefined };
    render(<BotCard bot={partial} />);
    expect(screen.getByText("—")).toBeInTheDocument();
  });

  it("falls back to risk_level=3 if not provided", () => {
    // @ts-expect-error testing fallback for required-missing
    const partial: Bot = { ...fullBot, risk_level: undefined };
    render(<BotCard bot={partial} />);
    expect(screen.getByLabelText(/risk level 3 of 5/i)).toBeInTheDocument();
  });
});
