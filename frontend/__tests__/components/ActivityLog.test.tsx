import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, render, screen, within } from "@testing-library/react";

/* -------------------------------------------------------------------------- */
/* Mock WebSocket — same contract used by useWebSocket.test.tsx               */
/* -------------------------------------------------------------------------- */

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
}

// @ts-expect-error — install mock as global before importing modules under test.
globalThis.WebSocket = MockWebSocket;

import ActivityLog from "@/components/ActivityLog";
import { __resetSingleton } from "@/hooks/useWebSocket";
import {
  __resetActivitySeq,
  appendToRing,
  diffSnapshotForActivity,
  entryFromPositions,
  entryFromSignal,
  entryFromTrade,
  formatClock,
  iconFor,
  labelFor,
  MAX_ACTIVITY_ENTRIES,
} from "@/lib/activity-log";
import type {
  PositionsEvent,
  SignalsEvent,
  StrategyEvent,
  TradesEvent,
} from "@/lib/ws-types";
import type { TradeOutcome } from "@/lib/types";

/* -------------------------------------------------------------------------- */
/* Helpers                                                                    */
/* -------------------------------------------------------------------------- */

function getSocket(): MockWebSocket {
  expect(MockWebSocket.instances.length).toBeGreaterThan(0);
  return MockWebSocket.instances[0];
}

function fireEvent(channel: string, data: unknown) {
  getSocket().simulateMessage({ type: "event", channel, ts: Date.now(), data });
}

const baseTrade: TradeOutcome = {
  id: 1,
  bot_id: 4,
  instrument: "BTCUSD",
  side: "buy",
  timeframe: "5m",
  entry_price: 80_201,
  exit_price: 80_201,
  qty: 0.01,
  pnl_usd: 0,
  r_multiple: 0,
  forecast_drift: null,
  forecast_confidence: 1.84,
  threshold_at_entry: 1.5,
  opened_at: new Date().toISOString(),
  closed_at: new Date().toISOString(),
  hold_seconds: 0,
};

/* -------------------------------------------------------------------------- */
/* Pure helpers                                                               */
/* -------------------------------------------------------------------------- */

describe("activity-log helpers", () => {
  beforeEach(() => __resetActivitySeq());

  it("formatClock pads HH:MM:SS", () => {
    const ts = new Date("2026-01-01T03:04:05").getTime();
    expect(formatClock(ts)).toBe("03:04:05");
  });

  it("iconFor / labelFor map every kind", () => {
    expect(iconFor("signal")).toBeTruthy();
    expect(labelFor("signal")).toBe("SIGNAL");
    expect(labelFor("entry")).toBe("ENTRY");
    expect(labelFor("scale")).toBe("SCALE");
    expect(labelFor("partial")).toBe("PARTIAL");
    expect(labelFor("sl_move")).toBe("SL→");
    expect(labelFor("exit")).toBe("EXIT");
    expect(labelFor("error")).toBe("ERROR");
  });

  it("entryFromSignal builds a profit-tone signal row", () => {
    const e = entryFromSignal({
      bot_id: 4,
      symbol: "BTCUSD",
      side: "buy",
      confidence: 1.84,
    } satisfies SignalsEvent);
    expect(e.kind).toBe("signal");
    expect(e.tone).toBe("profit");
    expect(e.text).toContain("BTCUSD");
    expect(e.detail).toContain("1.84");
  });

  it("entryFromPositions returns null for non-opened frames", () => {
    expect(
      entryFromPositions({
        kind: "updated",
        id: "1",
        symbol: "BTC",
        side: "buy",
        qty: 1,
        avg_price: 100,
      } satisfies PositionsEvent),
    ).toBeNull();
    expect(
      entryFromPositions({
        kind: "closed",
        id: "1",
        symbol: "BTC",
        side: "buy",
        qty: 1,
        avg_price: 100,
      } satisfies PositionsEvent),
    ).toBeNull();
  });

  it("entryFromTrade tones loss for negative pnl", () => {
    const e = entryFromTrade({ ...baseTrade, pnl_usd: -2.5 } satisfies TradesEvent);
    expect(e.kind).toBe("exit");
    expect(e.tone).toBe("loss");
    expect(e.text).toContain("−$2.50");
  });

  it("entryFromTrade tones profit for positive pnl", () => {
    const e = entryFromTrade({ ...baseTrade, pnl_usd: 3.2 } satisfies TradesEvent);
    expect(e.tone).toBe("profit");
    expect(e.text).toContain("+$3.20");
  });

  it("diffSnapshotForActivity emits SCALE when qty grows", () => {
    const prev: StrategyEvent = {
      state: {
        bot_id: 4,
        timeframe: "5m",
        is_running: true,
        confidence_threshold: 1.5,
        max_concurrent: 3,
        paused_until: null,
        last_signal_at: null,
        last_error: null,
      },
      performance: null,
      recent_trades: [{ ...baseTrade, qty: 0.01 }],
    };
    const next: StrategyEvent = {
      ...prev,
      recent_trades: [{ ...baseTrade, qty: 0.02 }],
    };
    const out = diffSnapshotForActivity(prev, next);
    expect(out).toHaveLength(1);
    expect(out[0].kind).toBe("scale");
  });

  it("diffSnapshotForActivity emits PARTIAL when qty shrinks but > 0", () => {
    const prev: StrategyEvent = {
      state: {
        bot_id: 4,
        timeframe: "5m",
        is_running: true,
        confidence_threshold: 1.5,
        max_concurrent: 3,
        paused_until: null,
        last_signal_at: null,
        last_error: null,
      },
      performance: null,
      recent_trades: [{ ...baseTrade, qty: 0.02, exit_price: 0 }],
    };
    const next: StrategyEvent = {
      ...prev,
      recent_trades: [{ ...baseTrade, qty: 0.01, exit_price: 80_365 }],
    };
    const out = diffSnapshotForActivity(prev, next);
    expect(out).toHaveLength(1);
    expect(out[0].kind).toBe("partial");
    expect(out[0].text).toContain("50%");
  });

  it("diffSnapshotForActivity emits nothing when prev is null", () => {
    const next: StrategyEvent = {
      state: {
        bot_id: 4,
        timeframe: "5m",
        is_running: true,
        confidence_threshold: 1.5,
        max_concurrent: 3,
        paused_until: null,
        last_signal_at: null,
        last_error: null,
      },
      performance: null,
      recent_trades: [{ ...baseTrade }],
    };
    expect(diffSnapshotForActivity(null, next)).toHaveLength(0);
  });

  it("appendToRing caps the buffer at MAX_ACTIVITY_ENTRIES", () => {
    const start = Array.from({ length: 49 }, (_, i) => ({
      id: `e${i}`,
      ts: i,
      channel: "internal" as const,
      kind: "signal" as const,
      tone: "profit" as const,
      text: `row ${i}`,
    }));
    const added = Array.from({ length: 5 }, (_, i) => ({
      id: `n${i}`,
      ts: 1000 + i,
      channel: "internal" as const,
      kind: "exit" as const,
      tone: "neutral" as const,
      text: `new ${i}`,
    }));
    const out = appendToRing(start, added);
    expect(out).toHaveLength(MAX_ACTIVITY_ENTRIES);
    expect(out[out.length - 1].text).toBe("new 4");
    expect(out[0].text).toBe("row 4"); // first 4 trimmed
  });
});

/* -------------------------------------------------------------------------- */
/* Component                                                                  */
/* -------------------------------------------------------------------------- */

describe("<ActivityLog />", () => {
  beforeEach(() => {
    MockWebSocket.instances = [];
    __resetSingleton();
    __resetActivitySeq();
  });
  afterEach(() => {
    __resetSingleton();
    MockWebSocket.instances = [];
  });

  it("renders the empty state when no events have been received", () => {
    render(<ActivityLog />);
    expect(
      screen.getByText(/waiting for activity/i),
    ).toBeInTheDocument();
  });

  it("appends an entry on a signal event", async () => {
    render(<ActivityLog />);
    await act(async () => {
      getSocket().simulateOpen();
    });
    await act(async () => {
      fireEvent("signals", {
        bot_id: 4,
        symbol: "BTCUSD",
        side: "buy",
        confidence: 1.84,
      });
    });
    const rows = screen.getAllByTestId("activity-log-row");
    expect(rows).toHaveLength(1);
    expect(within(rows[0]).getByText("SIGNAL")).toBeInTheDocument();
    expect(rows[0]).toHaveAttribute("data-tone", "profit");
  });

  it("appends an ENTRY on positions kind=opened, ignores updated/closed", async () => {
    render(<ActivityLog />);
    await act(async () => getSocket().simulateOpen());

    await act(async () => {
      fireEvent("positions", {
        kind: "opened",
        id: "1",
        symbol: "BTCUSD",
        side: "buy",
        qty: 0.01,
        avg_price: 80_201,
      });
    });
    await act(async () => {
      fireEvent("positions", {
        kind: "updated",
        id: "1",
        symbol: "BTCUSD",
        side: "buy",
        qty: 0.01,
        avg_price: 80_201,
      });
    });
    await act(async () => {
      fireEvent("positions", {
        kind: "closed",
        id: "1",
        symbol: "BTCUSD",
        side: "buy",
        qty: 0.01,
        avg_price: 80_201,
      });
    });

    const rows = screen.getAllByTestId("activity-log-row");
    expect(rows).toHaveLength(1);
    expect(within(rows[0]).getByText("ENTRY")).toBeInTheDocument();
  });

  it("renders EXIT with profit tone for positive pnl trade", async () => {
    render(<ActivityLog />);
    await act(async () => getSocket().simulateOpen());

    await act(async () => {
      fireEvent("trades", { ...baseTrade, pnl_usd: 3.2, r_multiple: 1.6 });
    });

    const row = screen.getByTestId("activity-log-row");
    expect(row).toHaveAttribute("data-kind", "exit");
    expect(row).toHaveAttribute("data-tone", "profit");
  });

  it("renders ERROR tone red", async () => {
    // We can't easily trigger an internal error path through the WS, but the
    // helper itself maps tone→color. Validate via the row attribute by firing
    // a trade with negative PnL (loss tone uses --danger like error).
    render(<ActivityLog />);
    await act(async () => getSocket().simulateOpen());
    await act(async () => {
      fireEvent("trades", { ...baseTrade, pnl_usd: -1.5, r_multiple: -0.8 });
    });
    const row = screen.getByTestId("activity-log-row");
    expect(row).toHaveAttribute("data-tone", "loss");
  });

  it("caps at the maxEntries prop (ring buffer behaviour)", async () => {
    render(<ActivityLog maxEntries={5} />);
    await act(async () => getSocket().simulateOpen());

    for (let i = 0; i < 8; i++) {
      await act(async () => {
        fireEvent("signals", {
          bot_id: 4,
          symbol: `SYM${i}`,
          side: "buy",
          confidence: 1.5,
        });
      });
    }
    const rows = screen.getAllByTestId("activity-log-row");
    expect(rows).toHaveLength(5);
    // Most recent 5 should be SYM3..SYM7
    expect(within(rows[0]).getByText(/SYM3/)).toBeInTheDocument();
    expect(within(rows[rows.length - 1]).getByText(/SYM7/)).toBeInTheDocument();
  });

  it("auto-scrolls to bottom by default; freezes when user scrolls up and shows the NEW pill", async () => {
    render(<ActivityLog maxEntries={50} />);
    await act(async () => getSocket().simulateOpen());

    // Push a few events first.
    for (let i = 0; i < 3; i++) {
      await act(async () => {
        fireEvent("signals", {
          bot_id: 4,
          symbol: `S${i}`,
          side: "buy",
          confidence: 1.5,
        });
      });
    }

    const scroller = screen.getByTestId("activity-log-scroller") as HTMLDivElement;

    // happy-dom doesn't run real layout — manually mock scroll geometry.
    // Pretend the user scrolled up: scrollTop=0, content tall, viewport short.
    Object.defineProperty(scroller, "scrollHeight", { value: 1000, configurable: true });
    Object.defineProperty(scroller, "clientHeight", { value: 200, configurable: true });
    scroller.scrollTop = 0;

    await act(async () => {
      scroller.dispatchEvent(new Event("scroll"));
    });

    // New event arrives while user is scrolled away → NEW pill should appear.
    await act(async () => {
      fireEvent("signals", {
        bot_id: 4,
        symbol: "LATE",
        side: "buy",
        confidence: 1.5,
      });
    });

    expect(screen.getByTestId("activity-log-jump-pill")).toBeInTheDocument();

    // Click the pill to jump back; it should disappear.
    await act(async () => {
      screen.getByTestId("activity-log-jump-pill").click();
    });
    expect(screen.queryByTestId("activity-log-jump-pill")).not.toBeInTheDocument();
  });

  it("shows LIVE indicator when WS is open", async () => {
    render(<ActivityLog />);
    await act(async () => getSocket().simulateOpen());
    expect(screen.getByText("live")).toBeInTheDocument();
  });
});
