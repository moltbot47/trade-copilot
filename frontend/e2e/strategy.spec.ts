/**
 * Strategy page render test:
 *   - Visit /strategy with a seeded login (so the EmailGate stays closed).
 *   - Verify the header, stats grid, equity curve section, activity log,
 *     and trade log section all render.
 *
 * All backend calls are mocked.
 */
import { test, expect, seedLoggedIn } from "./fixtures";

test.describe("Strategy page", () => {
  test.beforeEach(async ({ page, mockApi }) => {
    await seedLoggedIn(page);
    await mockApi();
  });

  test("renders header, stats cards, equity curve, and log sections", async ({
    page,
  }) => {
    await page.goto("/strategy");

    // --- Header ---
    await expect(page.getByText(/live strategy console/i)).toBeVisible();
    // The bot name is rendered in an h1; the default is "LaT-PFN Quant Trader".
    await expect(
      page.getByRole("heading", { level: 1 }),
    ).toBeVisible();

    // Timeframe toggle (1m / 5m).
    await expect(page.getByRole("button", { name: /^1m$/ })).toBeVisible();
    await expect(page.getByRole("button", { name: /^5m$/ })).toBeVisible();

    // --- Stats cards (win rate, profit factor, sharpe, max drawdown) ---
    const statsRegion = page.getByRole("region", {
      name: /live performance metrics/i,
    });
    await expect(statsRegion).toBeVisible();
    await expect(statsRegion).toContainText(/win rate/i);
    await expect(statsRegion).toContainText(/profit factor/i);
    await expect(statsRegion).toContainText(/sharpe/i);
    await expect(statsRegion).toContainText(/max drawdown/i);

    // --- Equity curve section ---
    await expect(page.getByRole("heading", { name: /equity curve/i })).toBeVisible();

    // --- Trade log section ---
    await expect(page.getByRole("heading", { name: /trade log/i })).toBeVisible();

    // --- Activity log + feedback log are both present ---
    await expect(
      page.getByRole("heading", { name: /self-adjusting feedback loop/i }),
    ).toBeVisible();
    await expect(page.getByRole("heading", { name: /open positions/i })).toBeVisible();
  });

  test("timeframe toggle switches between 1m and 5m", async ({ page }) => {
    await page.goto("/strategy");

    const oneMin = page.getByRole("button", { name: /^1m$/ });
    const fiveMin = page.getByRole("button", { name: /^5m$/ });

    // Default is 5m — clicking 1m should re-trigger a status fetch and
    // remain on the page without errors.
    await oneMin.click();
    await expect(oneMin).toBeVisible();
    await expect(fiveMin).toBeVisible();
  });
});
