import type {
  Bot,
  Subscription,
  AccountState,
  Signal,
  PnLPoint,
  ConnectResponse,
  StrategyTimeframe,
  StrategyStatusResponse,
  StrategyEquityResponse,
  StrategyState,
  AdvisorResponse,
  UserOut,
} from "./types";

const BASE_URL =
  process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000";

// localStorage key — purely a UI hint so we know whether to show the
// EmailGate. The real session lives in an HttpOnly cookie set by the
// backend. JS can't read it (and shouldn't).
const EMAIL_KEY = "userEmail";

function getUserEmail(): string | null {
  if (typeof window === "undefined") return null;
  return window.localStorage.getItem(EMAIL_KEY);
}

export function setUserEmail(email: string): void {
  if (typeof window === "undefined") return;
  window.localStorage.setItem(EMAIL_KEY, email);
}

export function clearUserEmail(): void {
  if (typeof window === "undefined") return;
  window.localStorage.removeItem(EMAIL_KEY);
}

export type ApiErrorKind =
  | "network"
  | "auth"
  | "validation"
  | "server"
  | "unknown";

export class ApiError extends Error {
  status?: number;
  kind: ApiErrorKind;
  // Structured `detail` payload when the backend returns one — e.g.
  // 409 select_account returns {code, message, accounts}. Callers can
  // narrow `detail.code` to render the right UX (picker, MFA prompt, ...)
  // instead of treating every 4xx as a flat error string.
  detail?: unknown;

  constructor(
    message: string,
    status?: number,
    kind: ApiErrorKind = "unknown",
    detail?: unknown,
  ) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.kind = kind;
    this.detail = detail;
  }
}

async function _doFetch(path: string, init: RequestInit): Promise<Response> {
  return fetch(`${BASE_URL}${path}`, {
    ...init,
    // Send/receive the tc_session cookie cross-origin.
    credentials: "include",
  });
}

async function request<T>(
  path: string,
  init: RequestInit = {}
): Promise<T> {
  const headers = new Headers(init.headers || {});
  if (!headers.has("Content-Type")) {
    headers.set("Content-Type", "application/json");
  }

  // Cold-start retry: Fly auto-stops idle machines and the first request
  // after sleep can take 5-15s OR fail with a network error while the
  // proxy waits for the machine to come up. We retry once with a small
  // backoff before surfacing a "network" error to the user.
  const MAX_TRIES = 2;
  let res: Response | null = null;
  for (let attempt = 1; attempt <= MAX_TRIES; attempt++) {
    try {
      const r = await _doFetch(path, { ...init, headers });
      // 502/503/504 from Fly's proxy during cold start — retry once.
      if ((r.status === 502 || r.status === 503 || r.status === 504) && attempt < MAX_TRIES) {
        await new Promise((resolve) => setTimeout(resolve, 1500));
        continue;
      }
      res = r;
      break;
    } catch {
      if (attempt < MAX_TRIES) {
        await new Promise((resolve) => setTimeout(resolve, 1500));
        continue;
      }
      throw new ApiError(
        "Cannot reach the server. Check your connection.",
        undefined,
        "network",
      );
    }
  }
  if (!res) {
    throw new ApiError(
      "Cannot reach the server. Check your connection.",
      undefined,
      "network",
    );
  }

  if (!res.ok) {
    // Try to extract the backend's structured error body before deciding
    // on a friendly message. We keep the raw `detail` payload around on
    // the ApiError so callers can branch on `detail.code` (e.g., the
    // select_account picker carries an accounts array we'd otherwise lose).
    let backendDetail: string | null = null;
    let rawDetail: unknown = undefined;
    try {
      const body = await res.json();
      if (body?.detail !== undefined) {
        rawDetail = body.detail;
        if (typeof body.detail === "string") backendDetail = body.detail;
        else if (Array.isArray(body.detail)) {
          // FastAPI/Pydantic 422 shape — detail is a list of field errors
          // like [{loc, msg, type}]. Surface the first one in a human
          // form: "server: must be letters, digits, dash, or underscore".
          // Falls back to count when we can't parse the shape we expect.
          const first = body.detail[0] as
            | { loc?: unknown[]; msg?: string }
            | undefined;
          if (first?.msg) {
            const loc = Array.isArray(first.loc)
              ? first.loc
                  .filter((p) => p !== "body")
                  .map(String)
                  .join(".")
              : "";
            backendDetail = loc ? `${loc}: ${first.msg}` : first.msg;
          } else {
            backendDetail = `Validation failed (${body.detail.length} error${
              body.detail.length === 1 ? "" : "s"
            })`;
          }
        } else if (typeof body.detail === "object" && body.detail !== null) {
          // Object form — surface its `message` as the user-facing string.
          const msg = (body.detail as { message?: unknown }).message;
          if (typeof msg === "string") backendDetail = msg;
        }
      } else if (body?.message) {
        backendDetail = String(body.message);
      }
    } catch {
      // body wasn't JSON — fall through to status-based messaging
    }

    const status = res.status;

    if (status === 401) {
      throw new ApiError(
        backendDetail || "Not signed in. Refresh and try again.",
        401,
        "auth",
        rawDetail,
      );
    }
    if (status === 403) {
      throw new ApiError(backendDetail || "Forbidden.", 403, "auth", rawDetail);
    }
    if (status >= 400 && status < 500) {
      throw new ApiError(
        backendDetail || `Request failed (HTTP ${status})`,
        status,
        "validation",
        rawDetail,
      );
    }
    if (status >= 500) {
      throw new ApiError(
        backendDetail || "Server error. Try again in a moment.",
        status,
        "server",
        rawDetail,
      );
    }
    // Catch-all for the rare 3xx-leak.
    throw new ApiError(
      backendDetail || `HTTP ${status}`,
      status,
      "unknown",
      rawDetail,
    );
  }
  // Some endpoints return no body (e.g. logout could).
  const text = await res.text();
  if (!text) return {} as T;
  try {
    return JSON.parse(text) as T;
  } catch {
    return {} as T;
  }
}

export const api = {
  // Auth
  login: (email: string) =>
    request<{ email: string; exp: number }>("/api/auth/login", {
      method: "POST",
      body: JSON.stringify({ email }),
    }),
  logout: () => request<{ status: string }>("/api/auth/logout", { method: "POST" }),
  me: () =>
    request<{ email: string; tradelocker_account_id: string | null; tradelocker_env: string }>(
      "/api/auth/me"
    ),

  // Bots
  getBots: () => request<Bot[]>("/api/bots"),
  getBot: (slug: string) => request<Bot>(`/api/bots/${slug}`),

  // TradeLocker
  connectTradeLocker: (
    email: string,
    password: string,
    server: string,
    env: "demo" | "live" = "demo",
    accountId?: string,
  ) =>
    request<ConnectResponse>("/api/tradelocker/connect", {
      method: "POST",
      body: JSON.stringify({
        // Trim defensively. Backend also strips whitespace before its
        // pattern validation, but doing it here too means the network
        // payload is already clean — easier to debug if anything goes
        // sideways later.
        email: email.trim(),
        password: password.trim(),
        server: server.trim(),
        env,
        // Backend omits the field rather than reading null/undefined.
        ...(accountId ? { account_id: accountId } : {}),
      }),
    }),
  getAccountState: () =>
    request<AccountState>("/api/tradelocker/account"),
  // Open positions with broker-side SL/TP status. Drives the dashboard's
  // "edit SL/TP" modal so the user can fix an unprotected position with
  // one click instead of digging into the broker UI.
  listOpenPositions: () =>
    request<{
      positions: Array<{
        id: string;
        symbol: string;
        side: "buy" | "sell" | string;
        qty: number;
        avg_price: number;
        unrealized_pl: number;
        has_sl: boolean;
        has_tp: boolean;
        stop_loss_id: string | null;
        take_profit_id: string | null;
      }>;
    }>("/api/tradelocker/positions"),
  modifyPositionLevels: (
    positionId: string,
    levels: { stop_loss?: number; take_profit?: number },
  ) =>
    request<{ status: string; detail?: string }>(
      `/api/tradelocker/positions/${positionId}/modify`,
      {
        method: "POST",
        body: JSON.stringify(levels),
      },
    ),
  // Account switcher — list every account on the user's linked TradeLocker
  // session + swap to a different one without re-entering credentials.
  listTradeLockerAccounts: () =>
    request<{
      current_account_id: string | null;
      env: string;
      accounts: Array<{
        account_id: string;
        acc_num: string;
        name?: string | null;
        balance: number;
        currency?: string | null;
        status?: string | null;
        type?: string | null;
        is_current: boolean;
      }>;
    }>("/api/tradelocker/accounts"),
  switchTradeLockerAccount: (accountId: string) =>
    request<{ status: string; detail?: string }>("/api/tradelocker/switch-account", {
      method: "POST",
      body: JSON.stringify({ account_id: accountId }),
    }),
  getAdvisor: (riskAppetite?: "conservative" | "balanced" | "aggressive") =>
    request<AdvisorResponse>(
      `/api/tradelocker/advisor${riskAppetite ? `?risk_appetite=${riskAppetite}` : ""}`,
    ),

  // User preferences
  getMe: () => request<UserOut>("/api/users/me"),
  updateMe: (patch: { risk_appetite?: "conservative" | "balanced" | "aggressive" }) =>
    request<UserOut>("/api/users/me", {
      method: "PATCH",
      body: JSON.stringify(patch),
    }),

  // Subscriptions
  getSubscriptions: () => request<Subscription[]>("/api/subscriptions"),
  subscribeToBot: (
    botId: number,
    aggression: number,
    allowedInstruments?: string[] | null,
  ) =>
    request<Subscription>("/api/subscriptions", {
      method: "POST",
      // Backend schema is `aggression_level` + `allowed_instruments` (snake_case).
      // We were previously sending `aggression` which the backend silently
      // ignored — falling back to the default of 5 regardless of slider.
      body: JSON.stringify({
        bot_id: botId,
        aggression_level: aggression,
        allowed_instruments:
          allowedInstruments && allowedInstruments.length > 0
            ? allowedInstruments
            : null,
      }),
    }),
  updateSubscription: (
    subId: number,
    patch: Partial<Pick<Subscription, "aggression_level" | "is_paused" | "allowed_instruments">>
  ) =>
    request<Subscription>(`/api/subscriptions/${subId}`, {
      method: "PATCH",
      body: JSON.stringify(patch),
    }),

  // Dashboard
  getDashboardPnL: () => request<PnLPoint[]>("/api/dashboard/pnl"),
  getPositions: () => request<Signal[]>("/api/dashboard/positions"),

  // Strategy
  getStrategyStatus: (botId: number, tf: StrategyTimeframe) =>
    request<StrategyStatusResponse>(
      `/api/strategy/status?bot_id=${botId}&timeframe=${tf}`
    ),
  getStrategyEquity: (botId: number) =>
    request<StrategyEquityResponse>(`/api/strategy/equity?bot_id=${botId}`),
  startStrategy: (
    botId: number,
    tf: StrategyTimeframe,
    symbols: string[],
    userEmails: string[]
  ) =>
    request<StrategyState>("/api/strategy/start", {
      method: "POST",
      body: JSON.stringify({
        bot_id: botId,
        timeframe: tf,
        symbols,
        user_emails: userEmails,
      }),
    }),
  stopStrategy: (botId: number, tf: StrategyTimeframe) =>
    request<StrategyState>("/api/strategy/stop", {
      method: "POST",
      body: JSON.stringify({ bot_id: botId, timeframe: tf }),
    }),
  // One-shot LaT-PFN analysis across the bot's instruments. No trades,
  // no DB write, no runner spawn — purely read-only "what would you do
  // right now?" data so the user can sanity-check before pressing START.
  analyzeStrategy: (botId: number, tf: StrategyTimeframe) =>
    request<{
      bot_id: number;
      bot_name: string;
      timeframe: StrategyTimeframe;
      threshold: number;
      target_rr: number;
      risk_appetite: string;
      user_email: string;
      elapsed_ms: number;
      // True when ANY symbol fell back to synthetic data. Drives the
      // "broker feed degraded" banner on /strategy.
      data_feed_degraded: boolean;
      synthetic_symbol_count: number;
      items: Array<{
        symbol: string;
        confidence?: number;
        drift_atr?: number;
        direction?: "buy" | "sell";
        entry?: number;
        stop_loss?: number;
        take_profit?: number;
        tp_pct?: number;
        rr?: number;
        fits_threshold?: boolean;
        atr?: number;
        elapsed_ms?: number;
        error?: string;
        // True when this symbol returned synthetic-fallback bars and the
        // analyzer refused to forecast on them.
        is_synthetic?: boolean;
      }>;
    }>("/api/strategy/analyze", {
      method: "POST",
      body: JSON.stringify({ bot_id: botId, timeframe: tf }),
    }),

  // Settings — per-user Discord webhook
  getDiscordWebhook: () =>
    request<{ has_webhook: boolean; masked: string | null }>(
      "/api/users/me/discord-webhook",
    ),
  setDiscordWebhook: (url: string | null) =>
    request<{ has_webhook: boolean; masked: string | null }>(
      "/api/users/me/discord-webhook",
      { method: "PUT", body: JSON.stringify({ url }) },
    ),
  testDiscordWebhook: () =>
    request<{ status: string }>("/api/users/me/discord-webhook/test", {
      method: "POST",
    }),

  // MFA
  getMfaStatus: () => request<{ enabled: boolean }>("/api/auth/mfa/status"),
  setupMfa: () =>
    request<{ secret: string; otpauth_uri: string; enabled: boolean }>(
      "/api/auth/mfa/setup",
      { method: "POST" },
    ),
  verifyMfa: (code: string) =>
    request<{ enabled: boolean }>("/api/auth/mfa/verify", {
      method: "POST",
      body: JSON.stringify({ code }),
    }),
  disableMfa: (code: string) =>
    request<{ enabled: boolean }>("/api/auth/mfa/disable", {
      method: "POST",
      body: JSON.stringify({ code }),
    }),

  // Audit log (own actions only)
  getAuditLog: (limit: number = 50) =>
    request<Array<{ ts: string; action: string; details: string; client_ip: string | null }>>(
      `/api/auth/audit-log?limit=${limit}`,
    ),

  // Panic stop (global pause for this user)
  getPanic: () => request<{ bot_paused: boolean }>("/api/users/me/panic"),
  setPanic: (paused: boolean) =>
    request<{ bot_paused: boolean }>("/api/users/me/panic", {
      method: "POST",
      body: JSON.stringify({ paused }),
    }),
};

export { getUserEmail };
