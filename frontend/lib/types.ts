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
  // Field names mirror the backend response (snake_case) exactly. Previously
  // this type used `aggression` / `paused`, which silently never matched the
  // server's response — every read returned undefined and the slider showed
  // its default `5` regardless of saved value.
  aggression_level: number; // 1-10
  is_paused: boolean;
  // None = subscribe to all of the bot's instruments (legacy default).
  allowed_instruments?: string[] | null;
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

export type RiskAppetite = "conservative" | "balanced" | "aggressive";

export type AdvisorPreset = {
  label: string;
  description: string;
  confidence_threshold: number;
  target_rr: number;
  max_margin_pct_per_pair: number;
  recommended_bots: string[];
};

export type AdvisorSuggestion = {
  symbol: string;
  init_margin_usd: number;
  max_lot_fit: number;
  margin_pct_of_balance: number;
  fits: boolean;
  warn: boolean;
  typical_daily_pct: number;
  note: string;
  broker_confirms_at_order_time: boolean;
};

export type AdvisorResponse = {
  connected: boolean;
  balance_usd: number;
  risk_appetite: RiskAppetite;
  preset: AdvisorPreset;
  concurrent_positions_at_min_lot: number;
  suggestions: AdvisorSuggestion[];
  skipped: string[];
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

// Backend responds with { status: "connected", detail: "<account_id>" }.
// Older code may also send { success, account_id, balance } — keep both
// optional so the UI is forgiving.
export type ConnectResponse = {
  status?: string;
  detail?: string;
  success?: boolean;
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
