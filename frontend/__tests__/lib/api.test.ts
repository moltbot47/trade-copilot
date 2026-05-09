import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { api, ApiError, setUserEmail, clearUserEmail } from "@/lib/api";

const okJson = (body: unknown) =>
  new Response(JSON.stringify(body), {
    status: 200,
    headers: { "Content-Type": "application/json" },
  });

describe("api wrapper", () => {
  let fetchSpy: ReturnType<typeof vi.spyOn>;

  beforeEach(() => {
    setUserEmail("alice@example.com");
    fetchSpy = vi.spyOn(globalThis, "fetch").mockResolvedValue(okJson({}));
  });

  afterEach(() => {
    clearUserEmail();
    fetchSpy.mockRestore();
  });

  it("getBots makes a GET to /api/bots with credentials and JSON content-type", async () => {
    fetchSpy.mockResolvedValueOnce(okJson([]));
    await api.getBots();
    expect(fetchSpy).toHaveBeenCalledOnce();
    const [url, init] = fetchSpy.mock.calls[0] as [string, RequestInit];
    expect(url).toContain("/api/bots");
    const headers = new Headers(init.headers || {});
    expect(headers.get("Content-Type")).toBe("application/json");
    // Auth is now via HttpOnly tc_session cookie — request must opt into
    // sending credentials cross-origin.
    expect(init.credentials).toBe("include");
  });

  it("connectTradeLocker POSTs JSON body and required fields", async () => {
    fetchSpy.mockResolvedValueOnce(
      okJson({ success: true, account_id: "1", balance: 1000 }),
    );
    await api.connectTradeLocker("trader@x.com", "secret123", "GENFX", "demo");
    const [url, init] = fetchSpy.mock.calls[0] as [string, RequestInit];
    expect(url).toContain("/api/tradelocker/connect");
    expect(init.method).toBe("POST");
    const body = JSON.parse(init.body as string);
    expect(body).toEqual({
      email: "trader@x.com",
      password: "secret123",
      server: "GENFX",
      env: "demo",
    });
  });

  it("getStrategyStatus encodes bot_id + timeframe in querystring", async () => {
    fetchSpy.mockResolvedValueOnce(
      okJson({
        state: { bot_id: 4, timeframe: "1m", is_running: false },
        performance: null,
        recent_trades: [],
      }),
    );
    await api.getStrategyStatus(4, "1m");
    const [url] = fetchSpy.mock.calls[0] as [string];
    expect(url).toContain("/api/strategy/status?bot_id=4&timeframe=1m");
  });

  it("getStrategyEquity targets /api/strategy/equity", async () => {
    fetchSpy.mockResolvedValueOnce(okJson({ points: [] }));
    await api.getStrategyEquity(7);
    const [url] = fetchSpy.mock.calls[0] as [string];
    expect(url).toContain("/api/strategy/equity?bot_id=7");
  });

  it("startStrategy POSTs symbols + emails", async () => {
    fetchSpy.mockResolvedValueOnce(okJson({ status: "started" }));
    await api.startStrategy(4, "1m", ["BTCUSD"], ["a@b.com"]);
    const [, init] = fetchSpy.mock.calls[0] as [string, RequestInit];
    const body = JSON.parse(init.body as string);
    expect(body).toEqual({
      bot_id: 4,
      timeframe: "1m",
      symbols: ["BTCUSD"],
      user_emails: ["a@b.com"],
    });
  });

  it("throws ApiError(kind=network) when fetch itself rejects", async () => {
    fetchSpy.mockRejectedValueOnce(new TypeError("fail to fetch"));
    await expect(api.getBots()).rejects.toMatchObject({
      name: "ApiError",
      kind: "network",
    });
  });

  it("propagates HTTPError detail from backend response body as validation kind", async () => {
    fetchSpy.mockResolvedValueOnce(
      new Response(JSON.stringify({ detail: "bot not found" }), {
        status: 404,
        headers: { "Content-Type": "application/json" },
      }),
    );
    await expect(api.getBot("missing")).rejects.toThrow("bot not found");
  });

  it("classifies 401 as ApiError kind=auth", async () => {
    fetchSpy.mockResolvedValueOnce(
      new Response(JSON.stringify({ detail: "session expired" }), {
        status: 401,
        headers: { "Content-Type": "application/json" },
      }),
    );
    await expect(api.getAccountState()).rejects.toMatchObject({
      name: "ApiError",
      kind: "auth",
      status: 401,
    });
  });

  it("classifies 500 as ApiError kind=server with default copy", async () => {
    fetchSpy.mockResolvedValueOnce(
      new Response("oops", { status: 500 }),
    );
    let caught: unknown = null;
    try {
      await api.getBots();
    } catch (e) {
      caught = e;
    }
    expect(caught).toBeInstanceOf(ApiError);
    expect((caught as ApiError).kind).toBe("server");
    expect((caught as ApiError).status).toBe(500);
  });

  it("classifies 4xx as ApiError kind=validation", async () => {
    fetchSpy.mockResolvedValueOnce(
      new Response(JSON.stringify({ detail: "bad input" }), {
        status: 422,
        headers: { "Content-Type": "application/json" },
      }),
    );
    await expect(api.getBots()).rejects.toMatchObject({
      kind: "validation",
      status: 422,
      message: "bad input",
    });
  });
});
