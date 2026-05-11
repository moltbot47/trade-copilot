/**
 * Login flow:
 *   1. Visit a page that mounts the EmailGate (the landing `/` doesn't,
 *      so we start at `/dashboard` where the gate auto-opens when no
 *      `userEmail` is in localStorage).
 *   2. Enter an email + submit.
 *   3. Verify the gate dismisses and the dashboard renders.
 *
 * The gate hits POST /api/auth/login (mocked) and then calls
 * window.location.reload() — we wait for the page to settle and re-assert.
 */
import { test, expect } from "./fixtures";

test.describe("Login flow", () => {
  test("user enters email and lands on the authenticated dashboard", async ({
    page,
    mockApi,
  }) => {
    await mockApi();

    // No userEmail in localStorage → EmailGate auto-opens.
    await page.goto("/dashboard");

    const dialog = page.getByRole("dialog", { name: /identify yourself/i });
    await expect(dialog).toBeVisible();

    await dialog.getByLabel("Email").fill("test@example.com");
    await dialog.getByRole("button", { name: /continue/i }).click();

    // After login the component calls window.location.reload().
    // The gate should be gone and the dashboard heading should be visible.
    await expect(
      page.getByRole("heading", { name: "dashboard", level: 1 }),
    ).toBeVisible();
    await expect(page.getByRole("dialog")).toHaveCount(0);

    // localStorage flag is set so the gate stays dismissed on subsequent loads.
    const stored = await page.evaluate(() => window.localStorage.getItem("userEmail"));
    expect(stored).toBe("test@example.com");
  });

  test("backend rejection surfaces an alert message in the gate", async ({
    page,
    mockApi,
  }) => {
    // The original "invalid email is rejected client-side" test asserted a
    // JS-side validator, but the <input type="email" required> field means
    // HTML5 native validation catches malformed input before submit() fires.
    // The realistic failure path users actually hit is a 4xx from the backend
    // (rate-limited, locked, etc.) — verify that path renders the alert.
    await mockApi({
      "POST /api/auth/login": async (route) =>
        route.fulfill({
          status: 429,
          headers: { "content-type": "application/json" },
          body: JSON.stringify({ detail: "Too many login attempts. Try again in a minute." }),
        }),
    });
    await page.goto("/dashboard");

    const dialog = page.getByRole("dialog", { name: /identify yourself/i });
    await expect(dialog).toBeVisible();

    await dialog.getByLabel("Email").fill("blocked@example.com");
    await dialog.getByRole("button", { name: /continue/i }).click();

    // friendlyError() renders the backend `detail` field into the role=alert <p>.
    await expect(dialog.getByRole("alert")).toContainText(/too many login attempts/i);
  });
});
