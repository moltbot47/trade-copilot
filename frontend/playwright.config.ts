import { defineConfig, devices } from "@playwright/test";

/**
 * Playwright config for Trade Copilot e2e tests.
 *
 * Tests run hermetically against a mocked backend (see `e2e/fixtures.ts`),
 * so a live FastAPI server is NOT required. The frontend dev server IS
 * required on the configured baseURL.
 *
 * Local: `npm run dev` in one terminal, `npm run e2e` in another.
 * CI:    the workflow boots the Next.js server via `webServer` below.
 *
 * Browsers must be installed once: `npx playwright install --with-deps chromium`.
 */
export default defineConfig({
  testDir: "./e2e",
  // Hermetic single-worker mode for deterministic test ordering.
  // No parallelism between tests within a file or across files.
  workers: 1,
  fullyParallel: false,
  // Fail the build on `test.only` left in source.
  forbidOnly: !!process.env.CI,
  // 1 retry in CI to absorb flaky network-mock timing; 0 locally.
  retries: process.env.CI ? 1 : 0,
  // Cap per-test wall time so a hung test doesn't stall the suite.
  timeout: 30_000,
  expect: {
    timeout: 5_000,
  },
  reporter: process.env.CI
    ? [["github"], ["html", { open: "never" }], ["list"]]
    : [["list"], ["html", { open: "never" }]],
  use: {
    baseURL: process.env.PLAYWRIGHT_BASE_URL || "http://localhost:3001",
    // Trace + screenshot only on failure to keep artifacts small.
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
    video: "off",
    // Tighter action timeout — these tests should be fast.
    actionTimeout: 5_000,
    navigationTimeout: 10_000,
  },
  projects: [
    {
      name: "chromium",
      use: { ...devices["Desktop Chrome"] },
    },
  ],
  // Auto-boot the Next.js dev server in CI so the workflow doesn't need
  // a separate "start server" step. Locally, we assume the dev runs.
  webServer: process.env.PLAYWRIGHT_SKIP_WEBSERVER
    ? undefined
    : {
        command: "npm run dev -- -p 3001",
        url: "http://localhost:3001",
        reuseExistingServer: !process.env.CI,
        timeout: 120_000,
        stdout: "pipe",
        stderr: "pipe",
      },
});
