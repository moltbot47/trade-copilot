/**
 * Donate page render test:
 *   - Visit /donate
 *   - Verify the "Buy us a coffee" anchor renders with the expected
 *     buymeacoffee.com href.
 */
import { test, expect } from "./fixtures";

test.describe("Donate page", () => {
  test("shows the Buy Me a Coffee link", async ({ page, mockApi }) => {
    await mockApi();
    await page.goto("/donate");

    await expect(page.getByRole("heading", { name: "donate" })).toBeVisible();

    // BMCButton renders an <a> with target=_blank pointing at buymeacoffee.com.
    // The default username is "dbutler" (see BMCButton.tsx and donate/page.tsx).
    const link = page.getByRole("link", {
      name: /buy.*coffee|open buy me a coffee/i,
    }).first();
    await expect(link).toBeVisible();
    await expect(link).toHaveAttribute(
      "href",
      /https:\/\/buymeacoffee\.com\/.+/,
    );
    await expect(link).toHaveAttribute("target", "_blank");
    await expect(link).toHaveAttribute("rel", /noopener/);
  });

  test("explanatory copy mentions hosting + R&D", async ({ page, mockApi }) => {
    await mockApi();
    await page.goto("/donate");

    await expect(page.getByText(/where the money goes/i)).toBeVisible();
    await expect(page.getByText(/hosting/i)).toBeVisible();
  });
});
