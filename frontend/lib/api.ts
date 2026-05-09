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

async function request<T>(
  path: string,
  init: RequestInit = {}
): Promise<T> {
  const headers = new Headers(init.headers || {});
  if (!headers.has("Content-Type")) {
    headers.set("Content-Type", "application/json");
  }

  let res: Response;
  try {
    res = await fetch(`${BASE_URL}${path}`, {
      ...init,
      headers,
      // Send/receive the tc_session cookie cross-origin.
      credentials: "include",
    });
  } catch (err) {
    // Network/CORS/DNS failure — fetch itself rejected. This is NOT a
    // signal that the backend is offline; it just means the browser
    // could not complete the round-trip.
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

  // Subscriptions
  getSubscriptions: () => request<Subscription[]>("/api/subscriptions"),
  subscribeToBot: (botId: number, aggression: number) =>
    request<Subscription>("/api/subscriptions", {
      method: "POST",
      body: JSON.stringify({ bot_id: botId, aggression }),
    }),
  updateSubscription: (
    subId: number,
    patch: Partial<Pick<Subscription, "aggression" | "paused">>
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
};

export { getUserEmail };
