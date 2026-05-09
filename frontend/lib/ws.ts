/**
 * WebSocketClient — Trade Copilot Wave 3C
 *
 * Conforms to docs/WS_PROTOCOL.md (v1, 2026-05-08).
 *
 * Auth strategy: COOKIE PRE-AUTH.
 *
 * Browsers send `tc_session` (HttpOnly) on the WS upgrade automatically (same
 * as fetch with `credentials:"include"`), so the backend can authenticate the
 * upgrade without a separate `auth` frame. The protocol spec lists "header"
 * and "query param" as alternatives — cookie is functionally equivalent to
 * the header path. If the server still requires the explicit `auth` frame, it
 * will reject after a short grace period; our backend (Wave 3A) is being
 * built to honor cookie auth on the upgrade and respond `auth_ok` proactively.
 *
 * If a token is provided via `opts.token` we also send the explicit `auth`
 * frame as belt-and-suspenders.
 *
 * Public surface:
 *   const ws = new WebSocketClient(url, { token? });
 *   ws.connect();
 *   ws.subscribe("account", (payload) => { ... });
 *   ws.unsubscribe("account", listener);
 *   await ws.command("strategy.start", { bot_id, timeframe, symbols });
 *   ws.onStatus((s) => { ... });
 *   ws.close();
 */

import type {
  ClientFrame,
  ServerFrame,
  WsChannel,
  WsStatus,
  ChannelListener,
  StatusListener,
  WsCommandName,
  ServerEventFrame,
} from "./ws-types";

const BACKOFF_SCHEDULE_MS = [1000, 2000, 4000, 8000, 16000, 30000] as const;
const STABILITY_RESET_MS = 60_000;
const HEARTBEAT_REPLY_TIMEOUT_MS = 10_000;
const COMMAND_TIMEOUT_MS = 15_000;

const DEV =
  typeof process !== "undefined" && process.env?.NODE_ENV !== "production";

function devLog(...args: unknown[]): void {
  if (!DEV) return;
  if (typeof console !== "undefined") {
    // eslint-disable-next-line no-console
    console.debug("[ws]", ...args);
  }
}

let cmdCounter = 0;
function nextCmdId(): string {
  cmdCounter = (cmdCounter + 1) >>> 0;
  return `c${Date.now().toString(36)}-${cmdCounter.toString(36)}`;
}

export interface WebSocketClientOptions {
  /** Optional explicit auth token. If omitted, cookie auth is used. */
  token?: string;
  /** Override the global WebSocket constructor (mostly for tests). */
  WebSocketImpl?: typeof WebSocket;
  /**
   * If true, resubscribe to channels automatically after reconnect (default).
   */
  resubscribeOnReconnect?: boolean;
}

interface PendingCommand {
  resolve: (result: unknown) => void;
  reject: (err: Error) => void;
  timeout: ReturnType<typeof setTimeout>;
}

export class WebSocketClient {
  readonly url: string;

  private readonly token: string | undefined;
  private readonly WSImpl: typeof WebSocket;
  private readonly resubscribeOnReconnect: boolean;

  private socket: WebSocket | null = null;
  private _status: WsStatus = "closed";
  private statusListeners = new Set<StatusListener>();

  /** channel -> set of listeners */
  private channelListeners = new Map<WsChannel, Set<ChannelListener>>();
  /** subscriptions we've requested (to replay after reconnect) */
  private wantedChannels = new Set<WsChannel>();
  /** subscriptions the server has acknowledged */
  private confirmedChannels = new Set<WsChannel>();

  private pendingCommands = new Map<string, PendingCommand>();

  private explicitlyClosed = false;
  private backoffIndex = 0;
  private reconnectTimer: ReturnType<typeof setTimeout> | null = null;
  private stabilityTimer: ReturnType<typeof setTimeout> | null = null;
  private heartbeatTimeoutTimer: ReturnType<typeof setTimeout> | null = null;

  constructor(url: string, opts: WebSocketClientOptions = {}) {
    this.url = url;
    this.token = opts.token;
    // Use injected impl, else global. Type cast: in SSR there is no WebSocket.
    this.WSImpl =
      opts.WebSocketImpl ||
      (typeof WebSocket !== "undefined"
        ? (WebSocket as typeof WebSocket)
        : (undefined as unknown as typeof WebSocket));
    this.resubscribeOnReconnect = opts.resubscribeOnReconnect !== false;
  }

  /* ----------------------------- status -------------------------------- */

  get status(): WsStatus {
    return this._status;
  }

  onStatus(listener: StatusListener): () => void {
    this.statusListeners.add(listener);
    listener(this._status);
    return () => this.statusListeners.delete(listener);
  }

  private setStatus(next: WsStatus): void {
    if (this._status === next) return;
    this._status = next;
    devLog("status →", next);
    this.statusListeners.forEach((l) => {
      try {
        l(next);
      } catch (err) {
        devLog("status listener threw", err);
      }
    });
  }

  /* ----------------------------- lifecycle ----------------------------- */

  connect(): void {
    if (!this.WSImpl) {
      devLog("no WebSocket impl available (SSR?) — connect() is a no-op");
      return;
    }
    this.explicitlyClosed = false;
    if (
      this.socket &&
      (this.socket.readyState === this.WSImpl.OPEN ||
        this.socket.readyState === this.WSImpl.CONNECTING)
    ) {
      return;
    }
    this.openSocket();
  }

  close(): void {
    this.explicitlyClosed = true;
    if (this.reconnectTimer) {
      clearTimeout(this.reconnectTimer);
      this.reconnectTimer = null;
    }
    if (this.stabilityTimer) {
      clearTimeout(this.stabilityTimer);
      this.stabilityTimer = null;
    }
    if (this.heartbeatTimeoutTimer) {
      clearTimeout(this.heartbeatTimeoutTimer);
      this.heartbeatTimeoutTimer = null;
    }
    // Reject all pending commands.
    this.pendingCommands.forEach((p) => {
      clearTimeout(p.timeout);
      p.reject(new Error("WebSocket closed"));
    });
    this.pendingCommands.clear();

    if (this.socket) {
      try {
        this.socket.close(1000, "client_close");
      } catch {
        // ignore
      }
      this.socket = null;
    }
    this.confirmedChannels.clear();
    this.setStatus("closed");
  }

  private openSocket(): void {
    this.setStatus(this.backoffIndex === 0 ? "connecting" : "reconnecting");
    let socket: WebSocket;
    try {
      socket = new this.WSImpl(this.url);
    } catch (err) {
      devLog("constructor threw", err);
      this.scheduleReconnect();
      return;
    }
    this.socket = socket;

    socket.addEventListener("open", () => this.handleOpen());
    socket.addEventListener("message", (e) => this.handleMessage(e));
    socket.addEventListener("close", (e) => this.handleClose(e));
    socket.addEventListener("error", () => devLog("socket error event"));
  }

  /* ----------------------------- send ---------------------------------- */

  send(frame: ClientFrame): void {
    if (!this.socket || this.socket.readyState !== this.WSImpl.OPEN) {
      devLog("send dropped — socket not open", frame);
      return;
    }
    try {
      this.socket.send(JSON.stringify(frame));
    } catch (err) {
      devLog("send failed", err);
    }
  }

  /* ----------------------------- subscriptions ------------------------- */

  subscribe<T = unknown>(
    channel: WsChannel,
    listener: ChannelListener<T>,
  ): () => void {
    let set = this.channelListeners.get(channel);
    if (!set) {
      set = new Set();
      this.channelListeners.set(channel, set);
    }
    set.add(listener as ChannelListener);

    if (!this.wantedChannels.has(channel)) {
      this.wantedChannels.add(channel);
      if (this.socket && this.socket.readyState === this.WSImpl.OPEN) {
        this.send({ type: "subscribe", channel });
      }
    }

    return () => this.unsubscribe(channel, listener as ChannelListener);
  }

  unsubscribe(channel: WsChannel, listener?: ChannelListener): void {
    const set = this.channelListeners.get(channel);
    if (set && listener) {
      set.delete(listener);
      if (set.size === 0) {
        this.channelListeners.delete(channel);
      }
    } else if (!listener) {
      this.channelListeners.delete(channel);
    }

    // Only send unsubscribe when no listeners remain.
    const remaining = this.channelListeners.get(channel);
    if (!remaining || remaining.size === 0) {
      this.wantedChannels.delete(channel);
      this.confirmedChannels.delete(channel);
      if (this.socket && this.socket.readyState === this.WSImpl.OPEN) {
        this.send({ type: "unsubscribe", channel });
      }
    }
  }

  /* ----------------------------- commands ------------------------------ */

  command<R = unknown>(
    name: WsCommandName,
    params: Record<string, unknown> = {},
  ): Promise<R> {
    return new Promise<R>((resolve, reject) => {
      if (!this.socket || this.socket.readyState !== this.WSImpl.OPEN) {
        reject(new Error("WebSocket not open"));
        return;
      }
      const cmd_id = nextCmdId();
      const timeout = setTimeout(() => {
        this.pendingCommands.delete(cmd_id);
        reject(new Error(`Command timeout: ${name}`));
      }, COMMAND_TIMEOUT_MS);
      this.pendingCommands.set(cmd_id, {
        resolve: resolve as (v: unknown) => void,
        reject,
        timeout,
      });
      this.send({ type: "command", cmd_id, name, params });
    });
  }

  /* ----------------------------- handlers ------------------------------ */

  private handleOpen(): void {
    devLog("open");
    this.setStatus("open");
    // Belt-and-suspenders: send explicit auth frame if a token was supplied.
    if (this.token) {
      this.send({ type: "auth", token: this.token });
    }
    // Replay subscriptions.
    if (this.resubscribeOnReconnect) {
      this.wantedChannels.forEach((channel) => {
        this.send({ type: "subscribe", channel });
      });
    }
    // Stability: if we stay connected for STABILITY_RESET_MS, reset backoff.
    if (this.stabilityTimer) clearTimeout(this.stabilityTimer);
    this.stabilityTimer = setTimeout(() => {
      this.backoffIndex = 0;
    }, STABILITY_RESET_MS);
  }

  private handleMessage(event: MessageEvent): void {
    let frame: ServerFrame;
    try {
      frame = JSON.parse(event.data as string) as ServerFrame;
    } catch (err) {
      devLog("non-JSON frame", event.data);
      return;
    }

    switch (frame.type) {
      case "ping": {
        this.send({ type: "pong", ts: frame.ts });
        return;
      }
      case "pong": {
        if (this.heartbeatTimeoutTimer) {
          clearTimeout(this.heartbeatTimeoutTimer);
          this.heartbeatTimeoutTimer = null;
        }
        return;
      }
      case "auth_ok": {
        devLog("auth_ok", frame.email);
        return;
      }
      case "auth_failed": {
        devLog("auth_failed", frame.reason);
        // server will close — let close handler decide reconnect
        return;
      }
      case "subscribed": {
        this.confirmedChannels.add(frame.channel);
        return;
      }
      case "unsubscribed": {
        this.confirmedChannels.delete(frame.channel);
        return;
      }
      case "event": {
        this.dispatchEvent(frame as ServerEventFrame);
        return;
      }
      case "command_ok": {
        const pending = this.pendingCommands.get(frame.cmd_id);
        if (pending) {
          clearTimeout(pending.timeout);
          this.pendingCommands.delete(frame.cmd_id);
          pending.resolve(frame.result);
        }
        return;
      }
      case "command_error": {
        const pending = this.pendingCommands.get(frame.cmd_id);
        if (pending) {
          clearTimeout(pending.timeout);
          this.pendingCommands.delete(frame.cmd_id);
          pending.reject(new Error(`${frame.code}: ${frame.message}`));
        }
        return;
      }
      case "error": {
        devLog("server error frame", frame);
        return;
      }
      default: {
        devLog("unknown frame", frame);
      }
    }
  }

  private dispatchEvent(frame: ServerEventFrame): void {
    const set = this.channelListeners.get(frame.channel);
    if (!set) return;
    set.forEach((listener) => {
      try {
        listener(frame.data, frame);
      } catch (err) {
        devLog("listener threw", err);
      }
    });
  }

  private handleClose(event: CloseEvent): void {
    devLog("close", event.code, event.reason);
    if (this.heartbeatTimeoutTimer) {
      clearTimeout(this.heartbeatTimeoutTimer);
      this.heartbeatTimeoutTimer = null;
    }
    if (this.stabilityTimer) {
      clearTimeout(this.stabilityTimer);
      this.stabilityTimer = null;
    }
    this.confirmedChannels.clear();
    this.socket = null;

    if (this.explicitlyClosed) {
      this.setStatus("closed");
      return;
    }
    // Auth failure or forbidden — don't loop forever; surface as closed.
    if (event.code === 4401 || event.code === 4403) {
      this.setStatus("closed");
      return;
    }
    this.scheduleReconnect();
  }

  private scheduleReconnect(): void {
    this.setStatus("reconnecting");
    const delay =
      BACKOFF_SCHEDULE_MS[
        Math.min(this.backoffIndex, BACKOFF_SCHEDULE_MS.length - 1)
      ];
    this.backoffIndex = Math.min(
      this.backoffIndex + 1,
      BACKOFF_SCHEDULE_MS.length - 1,
    );
    devLog(`reconnect in ${delay}ms`);
    if (this.reconnectTimer) clearTimeout(this.reconnectTimer);
    this.reconnectTimer = setTimeout(() => {
      this.reconnectTimer = null;
      if (this.explicitlyClosed) return;
      this.openSocket();
    }, delay);
  }
}

/* -------------------------------------------------------------------------- */
/* URL helper                                                                 */
/* -------------------------------------------------------------------------- */

export function defaultWsUrl(): string {
  const apiBase =
    (typeof process !== "undefined" && process.env?.NEXT_PUBLIC_API_URL) ||
    "http://localhost:8000";
  // Strip protocol and prepend ws/wss.
  try {
    const u = new URL(apiBase);
    const proto = u.protocol === "https:" ? "wss:" : "ws:";
    return `${proto}//${u.host}/ws`;
  } catch {
    return "ws://localhost:8000/ws";
  }
}
