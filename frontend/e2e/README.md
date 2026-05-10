# Playwright e2e tests

End-to-end tests for the Trade Copilot frontend. Tests run hermetically
against a mocked backend (see `fixtures.ts`) — no FastAPI server required.

## One-time setup

```bash
cd frontend
npm install
npx playwright install --with-deps chromium
```

`--with-deps` installs OS-level libraries required by Chromium (on Linux/CI).
On macOS it's a no-op beyond the browser binary itself.

## Running

```bash
# Headless run of all specs (boots the dev server automatically)
npm run e2e

# Interactive UI mode — recommended for development
npm run e2e:ui

# Step-through debugger
npm run e2e:debug

# Single file
npx playwright test e2e/login.spec.ts

# Single test
npx playwright test -g "user enters email"

# List discovered tests without running them
npx playwright test --list
```

## How tests stay hermetic

`fixtures.ts` exposes a `mockApi()` fixture. Each test calls it once, which
installs `page.route()` interceptors over every `/api/*` request and returns
canned JSON. WebSocket connections are aborted at the network layer, so the
WS client falls back to its disconnected state (which the UI handles fine).

This means:

- The FastAPI backend does NOT need to be running.
- No real network or DB state is touched.
- Tests are deterministic regardless of bot data on the live server.

To override a single endpoint for one test, pass a handler map:

```ts
await mockApi({
  "POST /api/auth/login": async (route) =>
    route.fulfill({ status: 400, body: JSON.stringify({ detail: "bad" }) }),
});
```

## Updating screenshots

There are no visual regression assertions yet (no `toHaveScreenshot()` calls).
If you add any:

```bash
# Generate / refresh the baseline images
npx playwright test --update-snapshots
```

## Spec inventory

| File              | Flow                                                      |
| ----------------- | --------------------------------------------------------- |
| `login.spec.ts`   | EmailGate dialog → submit → dashboard renders             |
| `bots.spec.ts`    | `/bots` → click Subscribe → redirect to `/strategy?bot=…` |
| `strategy.spec.ts`| `/strategy` renders header, stats, equity, logs           |
| `mfa.spec.ts`     | MFA setup → QR + secret → invalid code shows error        |
| `donate.spec.ts`  | `/donate` shows Buy Me a Coffee link                      |

`mfa.spec.ts` currently has two `test.fixme(...)` cases — the MFA UI is
not yet shipped. The mock-contract test inside it does run and asserts
the intended API shape so the backend team has a target.

## Debugging a failing test

1. Re-run with the inspector: `npx playwright test --debug e2e/<file>`
2. Open the HTML report after a failed run: `npx playwright show-report`
3. Traces are auto-saved on failure under `playwright-report/`.

## CI

The GitHub Actions job `e2e-tests` (defined in `.github/workflows/ci.yml`)
runs the full suite on every PR and push to `main`. It installs the
chromium browser, boots the Next.js dev server on `:3001` via the
`webServer` block in `playwright.config.ts`, and uploads the HTML report
as an artifact on failure.

## Adding a new test

1. Drop a new `*.spec.ts` file in `frontend/e2e/`.
2. Import from `./fixtures`:
   ```ts
   import { test, expect, seedLoggedIn } from "./fixtures";
   ```
3. Call `await mockApi()` in `beforeEach` or at the top of the test.
4. Use `seedLoggedIn(page)` to bypass the EmailGate on protected pages.
5. Keep each test under 5 seconds — no `waitForTimeout`, no real network.
