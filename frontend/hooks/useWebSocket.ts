"use client";

/**
 * useWebSocket — React hook wrapping the singleton WebSocketClient.
 *
 * SSR safety: returns an inert object during server render (no global
 * `window`). Connection is lazily kicked off on first client-side mount.
 */

import { useCallback, useEffect, useRef, useState } from "react";
import { WebSocketClient, defaultWsUrl } from "@/lib/ws";
import type {
  ChannelListener,
  WsChannel,
  WsCommandName,
  WsStatus,
} from "@/lib/ws-types";

/* -------------------------------------------------------------------------- */
/* Singleton                                                                  */
/* -------------------------------------------------------------------------- */

let singleton: WebSocketClient | null = null;

export function getWebSocketClient(): WebSocketClient | null {
  if (typeof window === "undefined") return null;
  if (!singleton) {
    singleton = new WebSocketClient(defaultWsUrl());
    singleton.connect();
  }
  return singleton;
}

/** Test-only: reset the singleton between tests. */
export function __resetSingleton(): void {
  if (singleton) {
    try {
      singleton.close();
    } catch {
      // ignore
    }
  }
  singleton = null;
}

/* -------------------------------------------------------------------------- */
/* Hook                                                                       */
/* -------------------------------------------------------------------------- */

export interface UseWebSocketResult {
  status: WsStatus;
  subscribe: <T = unknown>(
    channel: WsChannel,
    listener: ChannelListener<T>,
  ) => () => void;
  unsubscribe: (channel: WsChannel, listener?: ChannelListener) => void;
  command: <R = unknown>(
    name: WsCommandName,
    params?: Record<string, unknown>,
  ) => Promise<R>;
  lastError: Error | null;
}

const INERT: UseWebSocketResult = {
  status: "closed",
  subscribe: () => () => undefined,
  unsubscribe: () => undefined,
  command: () => Promise.reject(new Error("WebSocket unavailable (SSR)")),
  lastError: null,
};

export function useWebSocket(): UseWebSocketResult {
  const [status, setStatus] = useState<WsStatus>("closed");
  const [lastError, setLastError] = useState<Error | null>(null);
  const ownedSubscriptions = useRef<Array<() => void>>([]);

  useEffect(() => {
    const client = getWebSocketClient();
    if (!client) return;
    const off = client.onStatus((s) => setStatus(s));
    return () => {
      off();
      // Tear down any subscriptions registered through this hook instance.
      ownedSubscriptions.current.forEach((dispose) => {
        try {
          dispose();
        } catch {
          // ignore
        }
      });
      ownedSubscriptions.current = [];
    };
  }, []);

  const subscribe = useCallback(
    <T = unknown>(channel: WsChannel, listener: ChannelListener<T>) => {
      const client = getWebSocketClient();
      if (!client) return () => undefined;
      const dispose = client.subscribe<T>(channel, listener);
      ownedSubscriptions.current.push(dispose);
      return () => {
        dispose();
        ownedSubscriptions.current = ownedSubscriptions.current.filter(
          (d) => d !== dispose,
        );
      };
    },
    [],
  );

  const unsubscribe = useCallback(
    (channel: WsChannel, listener?: ChannelListener) => {
      const client = getWebSocketClient();
      if (!client) return;
      client.unsubscribe(channel, listener);
    },
    [],
  );

  const command = useCallback(
    <R = unknown>(
      name: WsCommandName,
      params: Record<string, unknown> = {},
    ): Promise<R> => {
      const client = getWebSocketClient();
      if (!client) return Promise.reject(new Error("WebSocket unavailable"));
      return client.command<R>(name, params).catch((err) => {
        const e = err instanceof Error ? err : new Error(String(err));
        setLastError(e);
        throw e;
      });
    },
    [],
  );

  if (typeof window === "undefined") return INERT;

  return { status, subscribe, unsubscribe, command, lastError };
}
