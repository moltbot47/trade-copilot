/**
 * Bot subscription flow:
 *   - Visit /bots
 *   - Click Subscribe on a bot card
 *   - Verify the page redirects to /strategy?bot=<slug>
 *   - Verify the bot name appears on the strategy page
 *
 * Tested against mocked /api/bots and /api/subscriptions.
 */
import { test, expect, MOCK_BOTS, seedLoggedIn } from "./fixtures";

test.describe("Bots marketplace", () => {
  test("renders bot cards from the API", async ({ page, mockApi }) => {
    await mockApi();
    await page.goto("/bots");

    await expect(
      page.getByRole("heading", { name: "strategy bots" }),
    ).toBeVisible();

    // Each bot from MOCK_BOTS should render a card with the bot name.
    for (const bot of MOCK_BOTS) {
      await expect(page.getByRole("heading", { name: bot.name })).toBeVisible();
    }
  });

  test("clicking Subscribe opens modal, confirm redirects to /strategy?bot=<slug>", async ({
    page,
    mockApi,
  }) => {
    // Seed an authenticated session so Subscribe opens the modal instead
    // of routing through EmailGate. Also override the tradelocker account
    // mock to `connected: true` so the modal's Confirm button is enabled
    // (otherwise it shows "connect broker first" and blocks submit).
    await seedLoggedIn(page, "test@example.com");
    await mockApi({
      "GET /api/tradelocker/account": async (route) =>
        route.fulfill({
          status: 200,
          body: JSON.stringify({
            balance: 50000,
            equity: 50000,
            open_pnl: 0,
            connected: true,
          }),
        }),
    });

    await page.goto("/bots");

    // Pick a bot deterministically (LaT-PFN Quant Trader — slug "latpfn-quant").
    const target = MOCK_BOTS.find((b) => b.slug === "latpfn-quant")!;
    const card = page
      .locator("article", { has: page.getByRole("heading", { name: target.name }) });
    await expect(card).toBeVisible();

    await card.getByRole("button", { name: /^subscribe/i }).click();

    // Subscribe is a modal-trigger button now (not a navigating link).
    // The modal renders an h2 "Subscribe to <bot.name>".
    const modalHeading = page.getByRole("heading", {
      level: 2,
      name: new RegExp(`Subscribe to ${target.name}`, "i"),
    });
    await expect(modalHeading).toBeVisible();

    // Confirm subscribe → POST /api/subscriptions → onSuccess() → window.location to /strategy
    await page.getByRole("button", { name: /^confirm subscribe/i }).click();
    await page.waitForURL(`**/strategy?bot=${target.slug}`);

    // Strategy page should resolve the bot from the URL param and render
    // its name in the main heading.
    await expect(
      page.getByRole("heading", { level: 1, name: new RegExp(target.name, "i") }),
    ).toBeVisible();
  });

  test("not-logged-in subscribe still routes to strategy (gate prompts there)", async ({
    page,
    mockApi,
  }) => {
    // No seedLoggedIn — BotCard takes the unauthenticated branch.
    await mockApi();
    await page.goto("/bots");

    const target = MOCK_BOTS[0];
    const card = page.locator("article", {
      has: page.getByRole("heading", { name: target.name }),
    });
    await card.getByRole("button", { name: /^subscribe/i }).click();

    await page.waitForURL(`**/strategy?bot=${target.slug}`);
  });
});
