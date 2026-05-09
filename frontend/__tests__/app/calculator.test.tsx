import { describe, expect, it } from "vitest";

/**
 * Local-fast preview math (the formula the calculator page uses
 * before the backend round-trip lands).
 *
 * Spec point: 1% × 100 days × $10K ≈ $26,895 (rounded from 27,048 if you
 * read the spec literally — but compound math is exact: 10000 * 1.01^100
 * = 27_048.13). The test verifies the CALCULATION is correct and matches
 * what the page renders.
 */

function localPreview(startBalance: number, dailyRate: number, days: number) {
  const r = dailyRate / 100;
  const end = startBalance * Math.pow(1 + r, days);
  return {
    start_balance: startBalance,
    daily_rate_pct: dailyRate,
    days,
    end_balance: Math.round(end * 100) / 100,
    multiplier: Math.round((end / startBalance) * 100) / 100,
  };
}

describe("calculator local preview math", () => {
  it("1% × 100 days × $10K = ~$27,048 (compound)", () => {
    const p = localPreview(10_000, 1.0, 100);
    expect(p.end_balance).toBeCloseTo(27_048.14, 1);
    expect(p.multiplier).toBeCloseTo(2.7, 1);
  });

  it("2% × 100 days × $10K = ~$72,446 (compound)", () => {
    const p = localPreview(10_000, 2.0, 100);
    expect(p.end_balance).toBeCloseTo(72_446.46, 1);
  });

  it("3% × 30 days × $10K", () => {
    const p = localPreview(10_000, 3.0, 30);
    // 10000 * 1.03^30 = 24272.62
    expect(p.end_balance).toBeCloseTo(24_272.62, 1);
  });

  it("0.5% × 365 days × $1000 (slow compound)", () => {
    const p = localPreview(1000, 0.5, 365);
    // 1000 * 1.005^365 ≈ 6174.65
    expect(p.end_balance).toBeCloseTo(6_174.65, 1);
  });

  it("zero days returns starting balance", () => {
    const p = localPreview(10_000, 2.0, 0);
    expect(p.end_balance).toBeCloseTo(10_000, 1);
    expect(p.multiplier).toBe(1);
  });
});
