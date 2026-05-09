export type Bot = {
  id: number;
  slug: string;
  name: string;
  description: string;
  // Backend returns these as percentages and "0.00" floats; defensive defaults below.
  backtest_win_rate?: number;
  backtest_profit_factor?: number;
  risk_level: number; // 1-5
  instruments_csv?: string;
  strategy_type?: string;
  is_active?: boolean;
  created_at?: string;
};

export type Subscription = {
  id: number;
  bot_id: number;
  bot_slug?: string;
  bot_name?: string;
  aggression: number; // 1-10
  paused: boolean;
  created_at?: string;
};

export type AccountState = {
  balance: number | null;
  equity: number | null;
  available_funds?: number | null;
  open_pnl: number | null;
  today_net?: number | null;
  positions_count?: number | null;
  currency?: string;
  account_id?: string;
  acc_num?: string;
  server?: string;
  env?: string;
  connected?: boolean;
};

export type Signal = {
  id: number;
  time: string;
  bot: string;
  instrument: string;
  side: "BUY" | "SELL" | string;
  entry: number;
  status: string;
};

export type PnLPoint = {
  time: string;
  pnl: number;
};

export type ConnectResponse = {
  success: boolean;
  account_id?: string;
  balance?: number;
  message?: string;
};

export type StrategyTimeframe = "1m" | "5m";

export interface StrategyState {
  bot_id: number;
  timeframe: StrategyTimeframe;
  is_running: boolean;
  confidence_threshold: number;
  max_concurrent: number;
  paused_until: string | null;
  last_signal_at: string | null;
  last_error: string | null;
}

export type FeedbackAction =
  | "tighten"
  | "loosen"
  | "hold"
  | "pause"
  | "warmup"
  | null;

export interface PerformanceSnapshot {
  snapshot_at: string;
  window_size: number;
  win_rate: number;
  profit_factor: number;
  sharpe: number;
  avg_r: number;
  max_drawdown_pct: number;
  total_pnl_usd: number;
  total_trades: number;
  threshold_after: number;
  threshold_before?: number;
  feedback_action: FeedbackAction;
}

export interface TradeOutcome {
  id: number;
  bot_id: number;
  instrument: string;
  side: "buy" | "sell";
  timeframe: string;
  entry_price: number;
  exit_price: number;
  qty: number;
  pnl_usd: number;
  r_multiple: number;
  forecast_drift: number | null;
  forecast_confidence: number | null;
  threshold_at_entry: number | null;
  opened_at: string;
  closed_at: string;
  hold_seconds: number;
}

export interface EquityPoint {
  ts: string;
  cumulative_r: number;
  cumulative_pnl: number;
}

export interface StrategyStatusResponse {
  state: StrategyState;
  performance: PerformanceSnapshot | null;
  recent_trades: TradeOutcome[];
  recent_snapshots?: PerformanceSnapshot[];
}

export interface StrategyEquityResponse {
  points: EquityPoint[];
}
