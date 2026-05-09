import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { WebSocketClient } from "@/lib/ws";

/**
 * Mock WebSocket. Behaviour tracking only what the client uses.
 */
class MockWebSocket {
  static CONNECTING = 0 as const;
  static OPEN = 1 as const;
  static CLOSING = 2 as const;
  static CLOSED = 3 as const;
  static instances: MockWebSocket[] = [];

  url: string;
  readyState: number = MockWebSocket.CONNECTING;
  sent: string[] = [];

  // Per-event listener arrays (matches addEventListener semantics).
  private listeners: Record<string, Array<(e: unknown) => void>> = {
    open: [],
    message: [],
    close: [],
    error: [],
  };

  constructor(url: string) {
    this.url = url;
    MockWebSocket.instances.push(this);
  }

  addEventListener(event: string, fn: (e: unknown) => void): void {
    if (!this.listeners[event]) this.listeners[event] = [];
    this.listeners[event].push(fn);
  }

  send(data: string): void {
    this.sent.push(data);
  }

  close(code = 1000, reason = ""): void {
    this.readyState = MockWebSocket.CLOSED;
    this.fire("close", { code, reason } as CloseEvent);
  }

  // --- test helpers ---

  fire(event: string, payload?: unknown): void {
    (this.listeners[event] || []).forEach((fn) => fn(payload));
  }

  simulateOpen(): void {
    this.readyState = MockWebSocket.OPEN;
    this.fire("open", {});
  }

  simulateMessage(obj: unknown): void {
    this.fire("message", { data: JSON.stringify(obj) } as MessageEvent);
  }

  simulateClose(code = 1006, reason = "abnormal"): void {
    this.readyState = MockWebSocket.CLOSED;
    this.fire("close", { code, reason } as CloseEvent);
  }

  sentFrames(): unknown[] {
    return this.sent.map((s) => JSON.parse(s));
  }
}

const URL = "ws://localhost:8000/ws";

describe("WebSocketClient", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    MockWebSocket.instances = [];
  });

  afterEach(() => {
    vi.useRealTimers();
    MockWebSocket.instances = [];
  });

  function makeClient(opts: { token?: string } = {}) {
    return new WebSocketClient(URL, {
      ...opts,
      WebSocketImpl: MockWebSocket as unknown as typeof WebSocket,
    });
  }

  it("emits status connecting → open on connect()", () => {
    const client = makeClient();
    const statusEvents: string[] = [];
    client.onStatus((s) => statusEvents.push(s));
    client.connect();
    const sock = MockWebSocket.instances[0];
    sock.simulateOpen();
    expect(statusEvents).toContain("connecting");
    expect(statusEvents).toContain("open");
  });

  it("sends explicit auth frame when token is provided", () => {
    const client = makeClient({ token: "jwt-abc" });
    client.connect();
    const sock = MockWebSocket.instances[0];
    sock.simulateOpen();
    const frames = sock.sentFrames();
    expect(frames[0]).toEqual({ type: "auth", token: "jwt-abc" });
  });

  it("does NOT send auth frame when no token (cookie pre-auth)", () => {
    const client = makeClient();
    client.connect();
    const sock = MockWebSocket.instances[0];
    sock.simulateOpen();
    const frames = sock.sentFrames();
    expect(frames.find((f) => (f as { type: string }).type === "auth")).toBeUndefined();
  });

  it("subscribe sends frame and dispatches incoming events to listener", () => {
    const client = makeClient();
    client.connect();
    const sock = MockWebSocket.instances[0];
    sock.simulateOpen();

    const received: unknown[] = [];
    client.subscribe("account", (payload) => received.push(payload));

    expect(sock.sentFrames()).toContainEqual({
      type: "subscribe",
      channel: "account",
    });

    sock.simulateMessage({
      type: "subscribed",
      channel: "account",
    });

    sock.simulateMessage({
      type: "event",
      channel: "account",
      ts: 123,
      data: { balance: 100, equity: 100 },
    });

    expect(received).toEqual([{ balance: 100, equity: 100 }]);
  });

  it("unsubscribe sends frame only when last listener for the channel is removed", () => {
    const client = makeClient();
    client.connect();
    const sock = MockWebSocket.instances[0];
    sock.simulateOpen();

    const l1 = vi.fn();
    const l2 = vi.fn();
    client.subscribe("account", l1);
    client.subscribe("account", l2);

    // First listener removed: still listeners → no unsubscribe sent.
    client.unsubscribe("account", l1);
    let unsubFrames = sock
      .sentFrames()
      .filter((f) => (f as { type: string }).type === "unsubscribe");
    expect(unsubFrames).toHaveLength(0);

    // Second removed: unsubscribe sent.
    client.unsubscribe("account", l2);
    unsubFrames = sock
      .sentFrames()
      .filter((f) => (f as { type: string }).type === "unsubscribe");
    expect(unsubFrames).toContainEqual({
      type: "unsubscribe",
      channel: "account",
    });
  });

  it("command resolves Promise on command_ok with matching cmd_id", async () => {
    const client = makeClient();
    client.connect();
    const sock = MockWebSocket.instances[0];
    sock.simulateOpen();

    const promise = client.command("strategy.start", { bot_id: 4, timeframe: "1m" });
    const sentCmd = sock.sentFrames().find(
      (f) => (f as { type: string }).type === "command",
    ) as { type: string; cmd_id: string; name: string };
    expect(sentCmd).toBeTruthy();
    expect(sentCmd.name).toBe("strategy.start");

    sock.simulateMessage({
      type: "command_ok",
      cmd_id: sentCmd.cmd_id,
      result: { is_running: true },
    });

    await expect(promise).resolves.toEqual({ is_running: true });
  });

  it("command rejects Promise on command_error", async () => {
    const client = makeClient();
    client.connect();
    const sock = MockWebSocket.instances[0];
    sock.simulateOpen();

    const promise = client.command("strategy.stop", {});
    const sentCmd = sock.sentFrames().find(
      (f) => (f as { type: string }).type === "command",
    ) as { type: string; cmd_id: string };

    sock.simulateMessage({
      type: "command_error",
      cmd_id: sentCmd.cmd_id,
      code: "validation",
      message: "bot_id required",
    });

    await expect(promise).rejects.toThrow(/validation: bot_id required/);
  });

  it("auto-replies to server ping with pong", () => {
    const client = makeClient();
    client.connect();
    const sock = MockWebSocket.instances[0];
    sock.simulateOpen();

    sock.simulateMessage({ type: "ping", ts: 12345 });

    const pongs = sock
      .sentFrames()
      .filter((f) => (f as { type: string }).type === "pong");
    expect(pongs).toContainEqual({ type: "pong", ts: 12345 });
  });

  it("reconnects with exponential backoff on abnormal close", () => {
    const client = makeClient();
    client.connect();
    const sock1 = MockWebSocket.instances[0];
    sock1.simulateOpen();

    const statusEvents: string[] = [];
    client.onStatus((s) => statusEvents.push(s));

    // Abnormal close → schedule reconnect.
    sock1.simulateClose(1006, "abnormal");
    expect(statusEvents).toContain("reconnecting");
    expect(MockWebSocket.instances).toHaveLength(1);

    // First retry at 1000ms.
    vi.advanceTimersByTime(1000);
    expect(MockWebSocket.instances).toHaveLength(2);

    // Second close should schedule a 2000ms retry.
    const sock2 = MockWebSocket.instances[1];
    sock2.simulateOpen();
    sock2.simulateClose(1006, "abnormal");
    vi.advanceTimersByTime(1999);
    expect(MockWebSocket.instances).toHaveLength(2);
    vi.advanceTimersByTime(1);
    expect(MockWebSocket.instances).toHaveLength(3);
  });

  it("does NOT reconnect after auth_failed close codes (4401)", () => {
    const client = makeClient();
    client.connect();
    const sock = MockWebSocket.instances[0];
    sock.simulateOpen();
    sock.simulateClose(4401, "unauthorized");

    vi.advanceTimersByTime(60_000);
    expect(MockWebSocket.instances).toHaveLength(1);
    expect(client.status).toBe("closed");
  });

  it("re-sends subscribe frames after reconnect", () => {
    const client = makeClient();
    client.connect();
    const sock1 = MockWebSocket.instances[0];
    sock1.simulateOpen();

    client.subscribe("strategy:1m", () => undefined);
    expect(sock1.sentFrames()).toContainEqual({
      type: "subscribe",
      channel: "strategy:1m",
    });

    sock1.simulateClose(1006, "abnormal");
    vi.advanceTimersByTime(1000);

    const sock2 = MockWebSocket.instances[1];
    sock2.simulateOpen();
    expect(sock2.sentFrames()).toContainEqual({
      type: "subscribe",
      channel: "strategy:1m",
    });
  });

  it("close() prevents reconnect", () => {
    const client = makeClient();
    client.connect();
    const sock = MockWebSocket.instances[0];
    sock.simulateOpen();
    client.close();

    // Even after a long delay, no new socket is opened.
    vi.advanceTimersByTime(60_000);
    expect(MockWebSocket.instances).toHaveLength(1);
    expect(client.status).toBe("closed");
  });

  it("rejects pending commands when close() is called", async () => {
    const client = makeClient();
    client.connect();
    const sock = MockWebSocket.instances[0];
    sock.simulateOpen();

    const p = client.command("strategy.start", {});
    client.close();
    await expect(p).rejects.toThrow(/closed/i);
  });

  it("ignores non-JSON message payloads without crashing", () => {
    const client = makeClient();
    client.connect();
    const sock = MockWebSocket.instances[0];
    sock.simulateOpen();

    expect(() => sock.fire("message", { data: "not-json" })).not.toThrow();
  });

  it("subscription.update command sends a frame matching the protocol", async () => {
    const client = makeClient();
    client.connect();
    const sock = MockWebSocket.instances[0];
    sock.simulateOpen();

    const promise = client.command("subscription.update", {
      subscription_id: 1,
      aggression_level: 7,
    });
    const sentCmd = sock.sentFrames().find(
      (f) => (f as { type: string }).type === "command",
    ) as { type: string; cmd_id: string; name: string; params: unknown };

    expect(sentCmd.name).toBe("subscription.update");
    expect(sentCmd.params).toEqual({
      subscription_id: 1,
      aggression_level: 7,
    });

    sock.simulateMessage({
      type: "command_ok",
      cmd_id: sentCmd.cmd_id,
      result: { id: 1, aggression: 7 },
    });
    await expect(promise).resolves.toEqual({ id: 1, aggression: 7 });
  });

  it("command rejects when socket is not open (REST fallback path indicator)", async () => {
    const client = makeClient();
    // No connect() / no simulateOpen() — socket stays closed.
    await expect(
      client.command("subscription.update", { subscription_id: 1 }),
    ).rejects.toThrow(/not open/i);
  });
});
