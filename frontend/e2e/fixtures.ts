/**
 * Shared Playwright fixtures and API mocks for Trade Copilot e2e tests.
 *
 * Why this exists: the frontend talks to a FastAPI backend at
 * NEXT_PUBLIC_API_URL (defaults to http://localhost:8000). To keep tests
 * hermetic and fast, we intercept those requests with `page.route()` and
 * return canned JSON. No real backend, no real network, no WebSocket.
 *
 * Usage in a spec:
 *
 *   import { test, expect } from "./fixtures";
 *
 *   test("something", async ({ page, mockApi }) => {
 *     await mockApi();                  // install default mocks
 *     await page.goto("/bots");
 *     // ...
 *   });
 *
 * Override a single endpoint by passing handlers:
 *
 *   await mockApi({
 *     "POST /api/auth/login": async (route) =>
 *       route.fulfill({ status: 400, body: JSON.stringify({ detail: "bad" }) }),
 *   });
 */
import { test as base, expect, Page, Route } from "@playwright/test";

// The frontend reads NEXT_PUBLIC_API_URL at build time. In dev it falls back
// to http://localhost:8000. We intercept both that origin and the relative
// /api/* path just in case.
export const API_ORIGIN = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000";

// ---------- Canned data --------------------------------------------------

export const MOCK_BOTS = [
  {
    id: 1,
    slug: "orb-breakout",
    name: "ORB Breakout",
    description: "Opening Range Breakout — fades the first 15 minutes.",
    backtest_win_rate: 58.2,
    backtest_profit_factor: 1.74,
    risk_level: 3,
    instruments_csv: "BTCUSD,ETHUSD",
    strategy_type: "breakout",
    is_active: true,
  },
  {
    id: 5,
    slug: "latpfn-quant",
    name: "LaT-PFN Quant Trader",
    description: "Zero-shot transformer forecasting with self-tuning threshold.",
    backtest_win_rate: 62.4,
    backtest_profit_factor: 2.11,
    risk_level: 4,
    instruments_csv: "BTCUSD,ETHUSD",
    strategy_type: "quant",
    is_active: true,
  },
];

export const MOCK_SUBSCRIPTION = {
  id: 100,
  bot_id: 5,
  bot_slug: "latpfn-quant",
  bot_name: "LaT-PFN Quant Trader",
  aggression: 5,
  paused: false,
  created_at: "2026-05-10T12:00:00Z",
};

export const MOCK_STRATEGY_STATUS = {
  state: {
    bot_id: 5,
    timeframe: "5m",
    is_running: false,
    confidence_threshold: 1.25,
    max_concurrent: 3,
    paused_until: null,
    last_signal_at: null,
    last_error: null,
  },
  performance: {
    snapshot_at: "2026-05-10T12:00:00Z",
    window_size: 20,
    win_rate: 0.6,
    profit_factor: 1.8,
    sharpe: 1.2,
    avg_r: 0.4,
    max_drawdown_pct: 0.08,
    total_pnl_usd: 1234.5,
    total_trades: 25,
    threshold_after: 1.25,
    feedback_action: "hold",
  },
  recent_trades: [],
  recent_snapshots: [],
};

export const MOCK_EQUITY = { points: [] };

// MFA endpoints are aspirational — the frontend doesn't ship them yet, but
// the e2e test exercises a (currently hypothetical) /api/auth/mfa/* flow so
// it's ready the moment those routes land.
export const MOCK_MFA_SETUP = {
  // dummy base32 secret + an SVG-encoded QR code data URL
  secret: "JBSWY3DPEHPK3PXP",
  qr_code:
    "data:image/svg+xml;base64,PHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciIHdpZHRoPSI1MCIgaGVpZ2h0PSI1MCI+PHJlY3Qgd2lkdGg9IjUwIiBoZWlnaHQ9IjUwIiBmaWxsPSJibGFjayIvPjwvc3ZnPg==",
  otpauth_url:
    "otpauth://totp/TradeCopilot:test@example.com?secret=JBSWY3DPEHPK3PXP&issuer=TradeCopilot",
};

// ---------- Mock installer ----------------------------------------------

type RouteHandler = (route: Route) => Promise<void> | void;
type Overrides = Record<string, RouteHandler>;

/**
 * Install the default canned-response mocks for /api/*.
 *
 * The matcher pattern is "METHOD /path/prefix" — e.g. "POST /api/auth/login".
 * Pass `overrides` to replace a default handler for one spec.
 */
async function installMocks(page: Page, overrides: Overrides = {}): Promise<void> {
  const escapeRegex = (s: string) => s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");

  // Match either the absolute API origin or any relative /api/* path
  // (in case the frontend is rebuilt with a different NEXT_PUBLIC_API_URL).
  const matchUrl = new RegExp(
    `^(?:${escapeRegex(API_ORIGIN)})?/api/.*`,
  );

  await page.route(matchUrl, async (route) => {
    const req = route.request();
    const url = new URL(req.url());
    const key = `${req.method()} ${url.pathname}`;

    // Exact + prefix override match.
    const handler =
      overrides[key] ||
      Object.entries(overrides).find(
        ([k]) => k.endsWith("*") && key.startsWith(k.slice(0, -1)),
      )?.[1];
    if (handler) {
      await handler(route);
      return;
    }

    // Default canned responses keyed by "METHOD PATH".
    const defaults: Record<string, () => Promise<void>> = {
      // Auth
      "POST /api/auth/login": async () =>
        route.fulfill({
          status: 200,
          headers: { "content-type": "application/json" },
          body: JSON.stringify({ email: "test@example.com", exp: 9999999999 }),
        }),
      "POST /api/auth/logout": async () =>
        route.fulfill({ status: 200, body: JSON.stringify({ status: "ok" }) }),
      "GET /api/auth/me": async () =>
        route.fulfill({
          status: 200,
          body: JSON.stringify({
            email: "test@example.com",
            tradelocker_account_id: null,
            tradelocker_env: "demo",
          }),
        }),

      // Bots
      "GET /api/bots": async () =>
        route.fulfill({
          status: 200,
          headers: { "content-type": "application/json" },
          body: JSON.stringify(MOCK_BOTS),
        }),

      // Subscriptions
      "GET /api/subscriptions": async () =>
        route.fulfill({
          status: 200,
          body: JSON.stringify([]),
        }),
      "POST /api/subscriptions": async () =>
        route.fulfill({
          status: 201,
          body: JSON.stringify(MOCK_SUBSCRIPTION),
        }),

      // Dashboard
      "GET /api/dashboard/pnl": async () =>
        route.fulfill({ status: 200, body: JSON.stringify([]) }),
      "GET /api/dashboard/positions": async () =>
        route.fulfill({ status: 200, body: JSON.stringify([]) }),

      // TradeLocker
      "GET /api/tradelocker/account": async () =>
        route.fulfill({
          status: 200,
          body: JSON.stringify({
            balance: 0,
            equity: 0,
            open_pnl: 0,
            connected: false,
          }),
        }),

      // Strategy
      "GET /api/strategy/status": async () =>
        route.fulfill({
          status: 200,
          body: JSON.stringify(MOCK_STRATEGY_STATUS),
        }),
      "GET /api/strategy/equity": async () =>
        route.fulfill({ status: 200, body: JSON.stringify(MOCK_EQUITY) }),
      "POST /api/strategy/start": async () =>
        route.fulfill({
          status: 200,
          body: JSON.stringify({ ...MOCK_STRATEGY_STATUS.state, is_running: true }),
        }),
      "POST /api/strategy/stop": async () =>
        route.fulfill({
          status: 200,
          body: JSON.stringify({ ...MOCK_STRATEGY_STATUS.state, is_running: false }),
        }),

      // MFA — aspirational endpoints, see comment above.
      "POST /api/auth/mfa/setup": async () =>
        route.fulfill({
          status: 200,
          body: JSON.stringify(MOCK_MFA_SETUP),
        }),
      "POST /api/auth/mfa/verify": async () =>
        route.fulfill({
          status: 400,
          body: JSON.stringify({ detail: "Invalid code. Try again." }),
        }),
    };

    // Try exact match first, then prefix match for path-prefixed endpoints.
    const exact = defaults[key];
    if (exact) {
      await exact();
      return;
    }
    const prefixed = Object.entries(defaults).find(([k]) => {
      const [m, p] = k.split(" ");
      return m === req.method() && url.pathname.startsWith(p);
    });
    if (prefixed) {
      await prefixed[1]();
      return;
    }

    // Unknown /api/* — return 404 so tests fail loudly instead of hitting net.
    await route.fulfill({
      status: 404,
      body: JSON.stringify({ detail: `unmocked: ${key}` }),
    });
  });

  // Also block any real WebSocket attempt — tests must not depend on WS.
  await page.route(/.*\/ws(\/.*)?$/, async (route) => {
    await route.abort();
  });
}

// ---------- Extended `test` --------------------------------------------

type Fixtures = {
  mockApi: (overrides?: Overrides) => Promise<void>;
};

export const test = base.extend<Fixtures>({
  mockApi: async ({ page }, use) => {
    await use((overrides?: Overrides) => installMocks(page, overrides));
  },
});

export { expect };

/**
 * Helper: seed localStorage so the EmailGate doesn't pop on protected pages.
 * Must be called AFTER an initial navigation (page must have an origin).
 */
export async function seedLoggedIn(page: Page, email = "test@example.com"): Promise<void> {
  await page.addInitScript((e) => {
    try {
      window.localStorage.setItem("userEmail", e);
    } catch {
      // ignore
    }
  }, email);
}
