import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { api, setUserEmail, clearUserEmail } from "@/lib/api";

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

  it("throws Backend offline on fetch network error", async () => {
    fetchSpy.mockRejectedValueOnce(new TypeError("fail to fetch"));
    await expect(api.getBots()).rejects.toThrow("Backend offline");
  });

  it("propagates HTTPError detail from backend response body", async () => {
    fetchSpy.mockResolvedValueOnce(
      new Response(JSON.stringify({ detail: "bot not found" }), {
        status: 404,
        headers: { "Content-Type": "application/json" },
      }),
    );
    await expect(api.getBot("missing")).rejects.toThrow("bot not found");
  });
});
