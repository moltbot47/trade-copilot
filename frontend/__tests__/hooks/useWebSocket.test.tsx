import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, render } from "@testing-library/react";
import { useEffect } from "react";

// Mock WebSocket placed on global before importing hooks.

class MockWebSocket {
  static CONNECTING = 0 as const;
  static OPEN = 1 as const;
  static CLOSING = 2 as const;
  static CLOSED = 3 as const;
  static instances: MockWebSocket[] = [];

  url: string;
  readyState: number = MockWebSocket.CONNECTING;
  sent: string[] = [];

  private listeners: Record<string, Array<(e: unknown) => void>> = {};

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
    this.fire("close", { code, reason });
  }
  fire(event: string, payload?: unknown): void {
    (this.listeners[event] || []).forEach((fn) => fn(payload));
  }
  simulateOpen(): void {
    this.readyState = MockWebSocket.OPEN;
    this.fire("open", {});
  }
  simulateMessage(obj: unknown): void {
    this.fire("message", { data: JSON.stringify(obj) });
  }
  sentFrames(): unknown[] {
    return this.sent.map((s) => JSON.parse(s));
  }
}

// @ts-expect-error — install mock as global.
globalThis.WebSocket = MockWebSocket;

// Now import the modules under test (after the mock is installed).
import { useWebSocket, __resetSingleton } from "@/hooks/useWebSocket";

describe("useWebSocket", () => {
  beforeEach(() => {
    MockWebSocket.instances = [];
    __resetSingleton();
  });
  afterEach(() => {
    __resetSingleton();
    MockWebSocket.instances = [];
  });

  function HookHarness({
    onMount,
  }: {
    onMount: (api: ReturnType<typeof useWebSocket>) => void;
  }) {
    const api = useWebSocket();
    useEffect(() => {
      onMount(api);
    }, [api, onMount]);
    return null;
  }

  it("subscribes on mount and disposes on unmount", async () => {
    const listener = vi.fn();
    let api: ReturnType<typeof useWebSocket> | null = null;
    const onMount = (a: ReturnType<typeof useWebSocket>) => {
      api = a;
    };

    const { unmount } = render(<HookHarness onMount={onMount} />);
    // Wait a microtask for effects.
    await act(async () => {
      await Promise.resolve();
    });

    expect(api).not.toBeNull();
    expect(MockWebSocket.instances).toHaveLength(1);
    const sock = MockWebSocket.instances[0];
    act(() => {
      sock.simulateOpen();
    });

    let dispose: () => void = () => undefined;
    act(() => {
      dispose = api!.subscribe("account", listener);
    });

    expect(sock.sentFrames()).toContainEqual({
      type: "subscribe",
      channel: "account",
    });

    act(() => {
      sock.simulateMessage({
        type: "event",
        channel: "account",
        ts: 1,
        data: { balance: 42 },
      });
    });
    expect(listener).toHaveBeenCalledWith({ balance: 42 }, expect.any(Object));

    // Disposing the subscription removes the listener.
    act(() => {
      dispose();
    });
    listener.mockClear();
    act(() => {
      sock.simulateMessage({
        type: "event",
        channel: "account",
        ts: 2,
        data: { balance: 99 },
      });
    });
    expect(listener).not.toHaveBeenCalled();

    unmount();
  });

  it("status updates as the socket opens", async () => {
    const seen: string[] = [];
    const StatusHarness = () => {
      const api = useWebSocket();
      seen.push(api.status);
      return null;
    };

    await act(async () => {
      render(<StatusHarness />);
    });

    const sock = MockWebSocket.instances[0];
    await act(async () => {
      sock.simulateOpen();
    });

    expect(seen).toContain("open");
  });

  it("hook unmount cleans up subscriptions registered through the hook", async () => {
    let api: ReturnType<typeof useWebSocket> | null = null;
    const onMount = (a: ReturnType<typeof useWebSocket>) => {
      api = a;
    };
    const { unmount } = render(<HookHarness onMount={onMount} />);
    await act(async () => {
      await Promise.resolve();
    });
    const sock = MockWebSocket.instances[0];
    act(() => sock.simulateOpen());

    const listener = vi.fn();
    act(() => {
      api!.subscribe("positions", listener);
    });

    // Sanity: subscribe sent.
    expect(sock.sentFrames()).toContainEqual({
      type: "subscribe",
      channel: "positions",
    });

    // Unmount → unsubscribe must fire.
    unmount();

    expect(sock.sentFrames()).toContainEqual({
      type: "unsubscribe",
      channel: "positions",
    });
  });
});
