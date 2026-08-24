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
  // Live performance from real (demo) closed trades. Percentages match
  // backtest_win_rate's scale (62.0 == 62%). null until the bot has trades.
  live_win_rate?: number | null;
  live_profit_factor?: number | null;
  live_total_trades?: number;
  // "live" → trust live_* fields; "backtest" → trust backtest_* fields;
  // "none" → no data yet, show a "collecting" state rather than 0.0%.
  stats_source?: "live" | "backtest" | "none";
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

export type UserOut = {
  id: number;
  email: string;
  created_at: string;
  is_active: boolean;
  max_daily_loss_pct: number;
  risk_appetite: RiskAppetite;
  tradelocker_account_id?: string | null;
  tradelocker_env: string;
};

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

// --- Partner onboarding ------------------------------------------------- //
export interface PartnerInvite {
  id: number;
  token: string;
  url_path: string;
  label: string;
  partner_name_hint: string | null;
  partner_email_hint: string | null;
  trading_account_id: number | null;
  account_label: string | null;
  account_env: string | null;
  auto_start: boolean;
  state: "active" | "used" | "expired" | "revoked";
  created_at: string | null;
  expires_at: string | null;
  used_at: string | null;
  submission_id: number | null;
}

export interface AstFinding {
  level: "block" | "warn";
  code: string;
  message: string;
  line: number;
}

export interface PartnerSubmission {
  id: number;
  invite_id: number;
  partner_name: string;
  partner_email: string;
  strategy_name: string;
  strategy_slug: string;
  instruments_csv: string;
  timeframe: string;
  params_json: string | null;
  backtest_notes: string | null;
  delivery_type: "source" | "http";
  endpoint_url: string | null;
  source_filename: string | null;
  has_source: boolean;
  source_code?: string | null;
  ast_scan: {
    ok: boolean;
    strategy_class: string | null;
    declared_name: string | null;
    findings: AstFinding[];
  } | null;
  status: "pending" | "approved" | "rejected";
  rejection_reason: string | null;
  approved_bot_id: number | null;
  created_at: string | null;
  reviewed_at: string | null;
}
