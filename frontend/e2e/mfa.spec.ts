/**
 * MFA setup flow:
 *   - From the settings/account view, click "Enable MFA"
 *   - Verify QR code + secret are shown
 *   - Enter a dummy code, verify an error message appears
 *
 * NOTE: The MFA UI is not yet shipped in the frontend (as of 2026-05-10
 * there is no /account or /settings page and no "Enable MFA" button).
 * These tests are written against the *intended* contract — POST
 * /api/auth/mfa/setup returns { secret, qr_code }, POST
 * /api/auth/mfa/verify returns 400 on a bad code — and use `test.fixme`
 * so they're discovered (visible in --list) but won't fail the suite
 * until the UI lands. When the UI lands:
 *   1. Replace the `test.fixme(...)` calls with `test(...)`.
 *   2. Update the selectors (page.goto path, button name) to match the
 *      real component.
 *
 * Why ship the file now? So the test inventory is complete and the
 * intended contract is checked into git — the next dev to wire up MFA
 * will see exactly what to assert.
 */
import { test, expect, MOCK_MFA_SETUP, seedLoggedIn } from "./fixtures";

test.describe("MFA setup flow", () => {
  test.beforeEach(async ({ page, mockApi }) => {
    await seedLoggedIn(page);
    await mockApi();
  });

  test.fixme(
    "Enable MFA shows QR code + secret",
    async ({ page }) => {
      // TODO(mfa): point to the real account/settings path once it lands.
      await page.goto("/account");

      await page.getByRole("button", { name: /enable mfa/i }).click();

      // QR code rendered as an <img> with the data URL from /mfa/setup.
      const qr = page.getByRole("img", { name: /mfa qr code/i });
      await expect(qr).toBeVisible();

      // Plaintext secret visible (for manual TOTP-app entry).
      await expect(page.getByText(MOCK_MFA_SETUP.secret)).toBeVisible();
    },
  );

  test.fixme(
    "invalid 6-digit code shows an error",
    async ({ page }) => {
      await page.goto("/account");
      await page.getByRole("button", { name: /enable mfa/i }).click();

      await page.getByLabel(/verification code/i).fill("000000");
      await page.getByRole("button", { name: /verify/i }).click();

      // The mocked /mfa/verify returns 400 with detail "Invalid code".
      await expect(page.getByRole("alert")).toContainText(/invalid code/i);
    },
  );

  // Sanity check that the *mocks* respond correctly — this is the only
  // test in this file that actually runs today, so the MFA contract is
  // at least exercised at the API-mock layer.
  test("MFA mock contract: /setup returns secret+qr, /verify rejects dummy code", async ({
    page,
  }) => {
    // Drive a fetch directly from the page context (no real backend).
    await page.goto("/");

    const setup = await page.evaluate(async () => {
      const r = await fetch("/api/auth/mfa/setup", { method: "POST" });
      return { status: r.status, body: await r.json() };
    });
    expect(setup.status).toBe(200);
    expect(setup.body).toMatchObject({
      secret: MOCK_MFA_SETUP.secret,
    });
    expect(setup.body.qr_code).toMatch(/^data:image\/svg\+xml/);

    const verify = await page.evaluate(async () => {
      const r = await fetch("/api/auth/mfa/verify", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ code: "000000" }),
      });
      return { status: r.status, body: await r.json() };
    });
    expect(verify.status).toBe(400);
    expect(verify.body.detail).toMatch(/invalid code/i);
  });
});
