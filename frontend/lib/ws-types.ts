// TypeScript types matching the WebSocket protocol contract.
// See: docs/WS_PROTOCOL.md (v1, 2026-05-08)
//
// Wave 3C — kept thin, value-typed; no runtime cost.

import type {
  StrategyState,
  PerformanceSnapshot,
  TradeOutcome,
  AccountState,
} from "./types";

/* -------------------------------------------------------------------------- */
/* Channels                                                                   */
/* -------------------------------------------------------------------------- */

export type WsChannel =
  | "account"
  | "positions"
  | "trades"
  | "signals"
  | "strategy:1m"
  | "strategy:5m";

/* -------------------------------------------------------------------------- */
/* Connection status                                                          */
/* -------------------------------------------------------------------------- */

export type WsStatus = "connecting" | "open" | "reconnecting" | "closed";

/* -------------------------------------------------------------------------- */
/* Outbound (client -> server) frames                                         */
/* -------------------------------------------------------------------------- */

export interface AuthFrame {
  type: "auth";
  token: string;
}

export interface SubscribeFrame {
  type: "subscribe";
  channel: WsChannel;
}

export interface UnsubscribeFrame {
  type: "unsubscribe";
  channel: WsChannel;
}

export interface CommandFrame<P = Record<string, unknown>> {
  type: "command";
  cmd_id: string;
  name: WsCommandName;
  params: P;
}

export interface PingFrame {
  type: "ping";
  ts: number;
}

export interface PongFrame {
  type: "pong";
  ts: number;
}

export type ClientFrame =
  | AuthFrame
  | SubscribeFrame
  | UnsubscribeFrame
  | CommandFrame
  | PingFrame
  | PongFrame;

/* -------------------------------------------------------------------------- */
/* Inbound (server -> client) frames                                          */
/* -------------------------------------------------------------------------- */

export interface AuthOkFrame {
  type: "auth_ok";
  email: string;
  user_id: number;
}

export interface AuthFailedFrame {
  type: "auth_failed";
  reason: string;
}

export interface SubscribedFrame {
  type: "subscribed";
  channel: WsChannel;
}

export interface UnsubscribedFrame {
  type: "unsubscribed";
  channel: WsChannel;
}

export interface ServerEventFrame<T = unknown> {
  type: "event";
  channel: WsChannel;
  ts?: number;
  data: T;
}

export interface CommandOkFrame<R = unknown> {
  type: "command_ok";
  cmd_id: string;
  result: R;
}

export interface CommandErrorFrame {
  type: "command_error";
  cmd_id: string;
  code: string;
  message: string;
}

export interface ErrorFrame {
  type: "error";
  code: "rate_limited" | "unauthorized" | "bad_frame" | string;
  message: string;
}

export type ServerFrame =
  | AuthOkFrame
  | AuthFailedFrame
  | SubscribedFrame
  | UnsubscribedFrame
  | ServerEventFrame
  | CommandOkFrame
  | CommandErrorFrame
  | ErrorFrame
  | PingFrame
  | PongFrame;

/* -------------------------------------------------------------------------- */
/* Channel-specific event payloads (helps consumers narrow `data`)            */
/* -------------------------------------------------------------------------- */

export type AccountEvent = AccountState;

export interface PositionsEvent {
  kind: "opened" | "closed" | "updated";
  id: string;
  symbol: string;
  side: "buy" | "sell" | string;
  qty: number;
  avg_price: number;
  unrealized_pl?: number;
}

export interface SignalsEvent {
  bot_id: number;
  symbol: string;
  side: "buy" | "sell" | string;
  confidence: number;
  ts?: string;
}

export type TradesEvent = TradeOutcome;

export interface StrategyEvent {
  state: StrategyState;
  performance: PerformanceSnapshot | null;
  recent_trades: TradeOutcome[];
  recent_snapshots?: PerformanceSnapshot[];
}

/* -------------------------------------------------------------------------- */
/* Commands                                                                   */
/* -------------------------------------------------------------------------- */

export type WsCommandName =
  | "strategy.start"
  | "strategy.stop"
  | "subscription.update";

export interface StrategyStartParams {
  bot_id: number;
  timeframe: "1m" | "5m";
  symbols: string[];
  user_emails?: string[];
}

export interface StrategyStopParams {
  bot_id: number;
  timeframe: "1m" | "5m";
}

export interface SubscriptionUpdateParams {
  subscription_id: number;
  aggression_level?: number;
  paused?: boolean;
}

/* -------------------------------------------------------------------------- */
/* Listener helpers                                                           */
/* -------------------------------------------------------------------------- */

export type ChannelListener<T = unknown> = (
  payload: T,
  frame: ServerEventFrame<T>,
) => void;

export type StatusListener = (status: WsStatus) => void;
