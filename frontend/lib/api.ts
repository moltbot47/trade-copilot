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

  constructor(message: string, status?: number, kind: ApiErrorKind = "unknown") {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.kind = kind;
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
    // on a friendly message.
    let backendDetail: string | null = null;
    try {
      const body = await res.json();
      if (body?.detail) backendDetail = String(body.detail);
      else if (body?.message) backendDetail = String(body.message);
    } catch {
      // body wasn't JSON — fall through to status-based messaging
    }

    const status = res.status;

    if (status === 401) {
      throw new ApiError(
        backendDetail || "Not signed in. Refresh and try again.",
        401,
        "auth",
      );
    }
    if (status === 403) {
      throw new ApiError(backendDetail || "Forbidden.", 403, "auth");
    }
    if (status >= 400 && status < 500) {
      throw new ApiError(
        backendDetail || `Request failed (HTTP ${status})`,
        status,
        "validation",
      );
    }
    if (status >= 500) {
      throw new ApiError(
        backendDetail || "Server error. Try again in a moment.",
        status,
        "server",
      );
    }
    // Catch-all for the rare 3xx-leak.
    throw new ApiError(
      backendDetail || `HTTP ${status}`,
      status,
      "unknown",
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
  ) =>
    request<ConnectResponse>("/api/tradelocker/connect", {
      method: "POST",
      body: JSON.stringify({ email, password, server, env }),
    }),
  getAccountState: () =>
    request<AccountState>("/api/tradelocker/account"),
  getAdvisor: (riskAppetite?: "conservative" | "balanced" | "aggressive") =>
    request<AdvisorResponse>(
      `/api/tradelocker/advisor${riskAppetite ? `?risk_appetite=${riskAppetite}` : ""}`,
    ),

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
