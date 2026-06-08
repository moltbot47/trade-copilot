"""SQLAlchemy ORM models for Trade Copilot."""
from __future__ import annotations

import enum
import secrets
from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.database import Base


def _generate_webhook_secret() -> str:
    """Default factory for Bot.webhook_secret — 256 bits of url-safe entropy."""
    return secrets.token_urlsafe(32)


class StrategyType(str, enum.Enum):
    orb = "orb"
    squeeze = "squeeze"
    stoch_hook = "stoch_hook"
    latpfn_momentum = "latpfn_momentum"
    latpfn_quant = "latpfn_quant"
    news_event = "news_event"


class CohortStatus(str, enum.Enum):
    open = "open"
    partial = "partial"  # partial-close has fired, trailing SL active
    closed = "closed"


class ExecutionStatus(str, enum.Enum):
    pending = "pending"
    filled = "filled"
    rejected = "rejected"
    error = "error"


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True, nullable=False)
    hashed_password: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    # TradeLocker (encrypted at rest)
    tradelocker_email: Mapped[str | None] = mapped_column(String(512), nullable=True)
    tradelocker_account_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    tradelocker_acc_num: Mapped[str | None] = mapped_column(String(16), nullable=True)
    tradelocker_token: Mapped[str | None] = mapped_column(Text, nullable=True)
    tradelocker_refresh_token: Mapped[str | None] = mapped_column(Text, nullable=True)
    tradelocker_server: Mapped[str | None] = mapped_column(String(64), nullable=True)
    tradelocker_env: Mapped[str] = mapped_column(String(8), default="demo")  # demo|live

    max_daily_loss_pct: Mapped[float] = mapped_column(Float, default=3.0)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    # User-declared risk appetite. Drives the tiny-account advisor's
    # recommended instrument list, threshold, and R:R target. Doesn't
    # *enforce* anything by itself — the user can still override which
    # pairs they subscribe to. Conservative is the safest preset; aggressive
    # loosens the entry threshold to capture more small-edge signals for
    # high-frequency compounding on tiny accounts.
    risk_appetite: Mapped[str] = mapped_column(String(16), default="balanced")

    # Tiny-account live guardrails. When tradelocker_env == "live" the runner
    # consults these to pre-empt over-leveraging.
    #   max_lot_override: hard cap on lot size (None = no override, use risk model)
    #   daily_kill_switch_pct: realized DD that halts trading until next UTC day
    #   max_concurrent_positions: cap on open cohorts (None = no override)
    #   exhaustion_filter_enabled: only fire entries that pass the wick/RSI filter
    max_lot_override: Mapped[float | None] = mapped_column(Float, nullable=True)
    daily_kill_switch_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    max_concurrent_positions: Mapped[int | None] = mapped_column(Integer, nullable=True)
    exhaustion_filter_enabled: Mapped[bool] = mapped_column(Boolean, default=False)

    # Global panic switch — when True, the runner skips ALL entry decisions
    # for this user. Settable via DB or POST /api/users/me/panic. Use as
    # one-button emergency stop without having to call /strategy/stop on
    # every (bot, timeframe) pair.
    bot_paused: Mapped[bool] = mapped_column(Boolean, default=False)

    # TOTP-based MFA. mfa_secret is the base32-encoded shared secret (stored
    # encrypted via the same Fernet key as the TL token); mfa_enabled gates
    # whether login requires a code. Setting up MFA writes mfa_secret first,
    # then verification flips mfa_enabled=True. Disabling MFA requires a
    # current valid TOTP code.
    mfa_secret: Mapped[str | None] = mapped_column(Text, nullable=True)
    mfa_enabled: Mapped[bool] = mapped_column(Boolean, default=False)

    # Circuit breaker — auto-halt after N consecutive losing trades. Cools
    # down for circuit_breaker_cooldown_minutes; during cooldown, runner
    # skips entries with skip_circuit_breaker. After cooldown the breaker
    # rearms — the next loss streak triggers it again.
    circuit_breaker_n_losses: Mapped[int | None] = mapped_column(Integer, nullable=True)
    circuit_breaker_cooldown_minutes: Mapped[int] = mapped_column(Integer, default=60)
    circuit_breaker_tripped_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    # Login lockout (OWASP ASVS V2): 5 consecutive failures locks the
    # account for 15 minutes. Reset on successful login. Tracked in DB
    # rather than memory so it survives restarts.
    failed_login_count: Mapped[int] = mapped_column(Integer, default=0)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    # Per-user Discord webhook for personalized signal posts. When set,
    # the runner posts that user's trade decisions to their own channel
    # rather than (or in addition to) the global webhook. Validated to
    # match a discord.com/api/webhooks/... shape.
    discord_webhook_url: Mapped[str | None] = mapped_column(Text, nullable=True)

    subscriptions: Mapped[list["Subscription"]] = relationship(back_populates="user", cascade="all, delete-orphan")
    executions: Mapped[list["Execution"]] = relationship(back_populates="user")


class Bot(Base):
    __tablename__ = "bots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    slug: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    description: Mapped[str] = mapped_column(Text, default="")
    strategy_type: Mapped[StrategyType] = mapped_column(Enum(StrategyType), nullable=False)
    backtest_win_rate: Mapped[float] = mapped_column(Float, default=0.0)
    backtest_profit_factor: Mapped[float] = mapped_column(Float, default=0.0)
    risk_level: Mapped[int] = mapped_column(Integer, default=3)  # 1-5
    instruments_csv: Mapped[str] = mapped_column(String(512), default="")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    # Per-bot HMAC secret for inbound webhook signature verification.
    # Auto-generated at row creation. Rotatable via /api/bots/{slug}/webhook/rotate.
    # NOT exposed in the public bot catalog — only via the authenticated owner endpoint.
    webhook_secret: Mapped[str] = mapped_column(
        String(64), nullable=False, default=_generate_webhook_secret
    )

    subscriptions: Mapped[list["Subscription"]] = relationship(back_populates="bot", cascade="all, delete-orphan")
    signals: Mapped[list["Signal"]] = relationship(back_populates="bot")


class Subscription(Base):
    __tablename__ = "subscriptions"
    __table_args__ = (UniqueConstraint("user_id", "bot_id", name="uq_user_bot"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True, nullable=False)
    bot_id: Mapped[int] = mapped_column(ForeignKey("bots.id"), index=True, nullable=False)
    aggression_level: Mapped[int] = mapped_column(Integer, default=5)  # 1-10
    is_paused: Mapped[bool] = mapped_column(Boolean, default=False)
    # Per-user instrument filter (CSV; NULL = inherit all of Bot.instruments_csv).
    # Validated against the parent bot's instruments on create/update so users
    # can't subscribe to instruments the bot doesn't actually trade.
    allowed_instruments: Mapped[str | None] = mapped_column(String(512), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    user: Mapped["User"] = relationship(back_populates="subscriptions")
    bot: Mapped["Bot"] = relationship(back_populates="subscriptions")


class Signal(Base):
    __tablename__ = "signals"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    bot_id: Mapped[int] = mapped_column(ForeignKey("bots.id"), index=True, nullable=False)
    instrument: Mapped[str] = mapped_column(String(32), nullable=False)
    side: Mapped[str] = mapped_column(String(8), nullable=False)  # buy|sell
    entry_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    stop_loss: Mapped[float | None] = mapped_column(Float, nullable=True)
    take_profit: Mapped[float | None] = mapped_column(Float, nullable=True)
    base_lot_size: Mapped[float] = mapped_column(Float, default=0.01)
    raw_payload: Mapped[str] = mapped_column(Text, default="{}")
    received_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)

    bot: Mapped["Bot"] = relationship(back_populates="signals")
    executions: Mapped[list["Execution"]] = relationship(back_populates="signal", cascade="all, delete-orphan")


class Execution(Base):
    __tablename__ = "executions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    signal_id: Mapped[int] = mapped_column(ForeignKey("signals.id"), index=True, nullable=False)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True, nullable=False)
    status: Mapped[ExecutionStatus] = mapped_column(Enum(ExecutionStatus), default=ExecutionStatus.pending)
    tradelocker_order_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    executed_lot_size: Mapped[float | None] = mapped_column(Float, nullable=True)
    fill_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    signal: Mapped["Signal"] = relationship(back_populates="executions")
    user: Mapped["User"] = relationship(back_populates="executions")


class StrategyState(Base):
    """Live state for a running strategy instance (per bot, per timeframe)."""

    __tablename__ = "strategy_state"
    __table_args__ = (UniqueConstraint("bot_id", "timeframe", name="uq_bot_tf"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    bot_id: Mapped[int] = mapped_column(ForeignKey("bots.id"), index=True, nullable=False)
    timeframe: Mapped[str] = mapped_column(String(8), nullable=False)  # 1m|5m
    is_running: Mapped[bool] = mapped_column(Boolean, default=False)
    confidence_threshold: Mapped[float] = mapped_column(Float, default=1.5)  # σ units, auto-tuned
    max_concurrent: Mapped[int] = mapped_column(Integer, default=3)
    paused_until: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_tick_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_signal_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class StrategyAccount(Base):
    """A dedicated TradeLocker demo account bound to ONE bot/strategy.

    This is the isolation-harness primitive. The user picked:
      - provisioning: multiple sub-accounts under ONE Genesis FX login
      - isolation unit: per bot/strategy

    So credentials (token/refresh) live on the owning ``User`` row (shared
    login, refreshed by the existing token-refresh helper) and this row
    only pins WHICH sub-account this strategy trades on:
    ``tradelocker_account_id`` + ``tradelocker_acc_num``.

    Two hard DB guarantees enforce true isolation:
      - ``uq_strategy_account_bot``: one account per bot (can't double-bind a
        strategy).
      - ``uq_strategy_account_tlacct``: one strategy per demo account (two
        strategies can never share a sub-account, so equity/drawdown/Sharpe
        never cross-contaminate).
    """

    __tablename__ = "strategy_accounts"
    __table_args__ = (
        UniqueConstraint("bot_id", name="uq_strategy_account_bot"),
        UniqueConstraint(
            "tradelocker_account_id", name="uq_strategy_account_tlacct"
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    bot_id: Mapped[int] = mapped_column(
        ForeignKey("bots.id"), index=True, nullable=False
    )
    # The TradeLocker login that owns this sub-account. Supplies the
    # access/refresh tokens (shared across all isolation accounts under the
    # same login per the chosen provisioning model).
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id"), index=True, nullable=False
    )
    label: Mapped[str] = mapped_column(String(120), default="")

    # The specific sub-account this strategy trades on.
    tradelocker_account_id: Mapped[str] = mapped_column(
        String(64), nullable=False
    )
    tradelocker_acc_num: Mapped[str] = mapped_column(String(16), default="1")
    tradelocker_env: Mapped[str] = mapped_column(String(8), default="demo")

    # Which timeframe this isolated strategy runs at (LaT-PFN/quant bars).
    timeframe: Mapped[str] = mapped_column(String(8), default="1m")

    # Staggered start: the IsolatedRunner sleeps this many seconds (+ jitter)
    # before its first tick so N runners don't all hammer the broker on the
    # same second.
    start_offset_seconds: Mapped[int] = mapped_column(Integer, default=0)

    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow
    )


class TradeOutcome(Base):
    """Closed trade record for the feedback loop."""

    __tablename__ = "trade_outcomes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    bot_id: Mapped[int] = mapped_column(ForeignKey("bots.id"), index=True, nullable=False)
    # Set ONLY by the isolation harness. NULL for copy-runner outcomes, so
    # the isolation equity curve filters cleanly on this and never mixes in
    # multi-user copy trades.
    strategy_account_id: Mapped[int | None] = mapped_column(
        ForeignKey("strategy_accounts.id"), index=True, nullable=True
    )
    signal_id: Mapped[int | None] = mapped_column(ForeignKey("signals.id"), nullable=True)
    instrument: Mapped[str] = mapped_column(String(32), nullable=False)
    side: Mapped[str] = mapped_column(String(8), nullable=False)
    timeframe: Mapped[str] = mapped_column(String(8), nullable=False)

    entry_price: Mapped[float] = mapped_column(Float, nullable=False)
    exit_price: Mapped[float] = mapped_column(Float, nullable=False)
    qty: Mapped[float] = mapped_column(Float, nullable=False)
    pnl_usd: Mapped[float] = mapped_column(Float, nullable=False)
    r_multiple: Mapped[float] = mapped_column(Float, default=0.0)  # PnL / risked

    # Forecast context at entry (for post-mortem)
    forecast_drift: Mapped[float | None] = mapped_column(Float, nullable=True)
    forecast_confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    threshold_at_entry: Mapped[float | None] = mapped_column(Float, nullable=True)

    opened_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)
    closed_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)
    hold_seconds: Mapped[int] = mapped_column(Integer, default=0)
    mae: Mapped[float] = mapped_column(Float, default=0.0)  # max adverse excursion
    mfe: Mapped[float] = mapped_column(Float, default=0.0)  # max favorable excursion


class PerformanceSnapshot(Base):
    """Rolling stats snapshot — one row per bot per N trades."""

    __tablename__ = "performance_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    bot_id: Mapped[int] = mapped_column(ForeignKey("bots.id"), index=True, nullable=False)
    snapshot_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)
    window_size: Mapped[int] = mapped_column(Integer, default=20)

    win_rate: Mapped[float] = mapped_column(Float, default=0.0)
    profit_factor: Mapped[float] = mapped_column(Float, default=0.0)
    sharpe: Mapped[float] = mapped_column(Float, default=0.0)
    avg_r: Mapped[float] = mapped_column(Float, default=0.0)
    max_drawdown_pct: Mapped[float] = mapped_column(Float, default=0.0)
    total_pnl_usd: Mapped[float] = mapped_column(Float, default=0.0)
    total_trades: Mapped[int] = mapped_column(Integer, default=0)

    threshold_after: Mapped[float] = mapped_column(Float, default=1.5)
    feedback_action: Mapped[str | None] = mapped_column(String(32), nullable=True)  # tighten|loosen|pause|hold


class TradingAccount(Base):
    """A delegated trading account — one row per broker account that can be
    traded on behalf of its owner.

    Today the only attached broker is TradeLocker. The credentials
    (token + refresh) live on the OWNER's user row (one Genesis login per
    user); this row pins the sub-account id and provides the access-grant
    surface so an employee can be invited to trade it without sharing the
    owner's password.

    Why a separate table from User.tradelocker_* fields?
      - A user can own multiple accounts (main, demo, prop-firm sub).
      - Access can be granted to other users WITHOUT giving them broker creds.
      - Audit log records actor (who pressed the button) + account (whose
        money it was) — impossible if "account" only equals "user".
    """
    __tablename__ = "trading_accounts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    owner_user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id"), index=True, nullable=False
    )
    label: Mapped[str] = mapped_column(String(120), default="")
    # Pins which broker sub-account this row represents.
    tradelocker_account_id: Mapped[str] = mapped_column(String(64), nullable=False)
    tradelocker_acc_num: Mapped[str] = mapped_column(String(16), default="1")
    tradelocker_env: Mapped[str] = mapped_column(String(8), default="demo")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    __table_args__ = (
        UniqueConstraint(
            "owner_user_id",
            "tradelocker_account_id",
            name="uq_tradingaccount_owner_tlacct",
        ),
    )


class AccountAccessRole(str, enum.Enum):
    """Roles a grant can confer on a trading account.

    trader → may place orders, modify SL/TP, close positions, see state
    viewer → may see state + positions but NOT place / modify / close
    """
    trader = "trader"
    viewer = "viewer"


class AccountAccessGrant(Base):
    """Authorization grant: `grantee_user_id` may act on `account_id` with `role`.

    Owner-self access is implicit (the owner doesn't need a grant on his own
    account); this table is for delegating to OTHER users.

    Lifecycle:
      - Created by the account owner via /api/accounts/{id}/grants
      - Optionally expires at `expires_at`
      - Revoked by setting `revoked_at` (NEVER delete — preserves audit trail)

    Look-up rule used by DOM endpoints:
      may_trade(user, account) :=
        account.owner_user_id == user.id
        OR EXISTS grant WHERE account_id = account.id
                       AND grantee_user_id = user.id
                       AND role IN ('trader')
                       AND revoked_at IS NULL
                       AND (expires_at IS NULL OR expires_at > now())
    """
    __tablename__ = "account_access_grants"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_id: Mapped[int] = mapped_column(
        ForeignKey("trading_accounts.id"), index=True, nullable=False
    )
    grantee_user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id"), index=True, nullable=False
    )
    role: Mapped[AccountAccessRole] = mapped_column(
        Enum(AccountAccessRole), default=AccountAccessRole.trader, nullable=False
    )
    granted_by_user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    # Per-grantee risk caps — server-side enforcement on /dom/order.
    # NULL = no cap (uses the owner's account limits only).
    max_lot_per_order: Mapped[float | None] = mapped_column(Float, nullable=True)
    max_daily_loss_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    # Comma-separated whitelist of instrument symbols. NULL/empty = no filter.
    allowed_instruments_csv: Mapped[str | None] = mapped_column(
        String(512), nullable=True
    )

    # Partner webhook — every slippage_record lifecycle event (signal, fill,
    # close, rejected) on accounts the grantee can see is teed to this URL
    # signed with the grantee's HMAC secret. Lets the partner keep an
    # independent audit mirror outside our platform (the trust anchor for
    # profit-share arrangements). NULL on either field = webhook disabled
    # for this grant. The secret is Fernet-encrypted at rest using the
    # same key as tradelocker_token.
    partner_webhook_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    partner_webhook_secret_encrypted: Mapped[str | None] = mapped_column(
        Text, nullable=True
    )


class AuditLog(Base):
    """Append-only audit trail for security-sensitive actions.

    OWASP ASVS V7 requires logging of authentication events, credential
    changes, and authorization decisions. We also log changes to risk
    settings (cap, kill switch, panic toggle) so forensic review can
    answer "who changed what, when, from where".

    NEVER store secrets in `details` — only references and field names.
    """
    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)
    user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id"), index=True, nullable=True,
    )
    actor_email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # Optional account context — set on order/position events so an owner can
    # filter audit log by "what happened on my account #X" regardless of which
    # user clicked the button.
    account_id: Mapped[int | None] = mapped_column(
        ForeignKey("trading_accounts.id"), index=True, nullable=True,
    )
    action: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    details: Mapped[str] = mapped_column(Text, default="{}")
    client_ip: Mapped[str | None] = mapped_column(String(64), nullable=True)


class Cohort(Base):
    """A pyramided cohort: an entry leg + zero or more scale-in legs.

    Persists across runner restart so a quant strategy can resume management
    of in-flight positions. Children (CohortLeg) reference this row.
    """

    __tablename__ = "cohorts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    bot_id: Mapped[int] = mapped_column(ForeignKey("bots.id"), index=True, nullable=False)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True, nullable=False)
    instrument: Mapped[str] = mapped_column(String(32), nullable=False)
    side: Mapped[str] = mapped_column(String(8), nullable=False)  # buy|sell
    timeframe: Mapped[str] = mapped_column(String(8), nullable=False)
    status: Mapped[CohortStatus] = mapped_column(Enum(CohortStatus), default=CohortStatus.open)

    # ATR sized at entry — fixed for the cohort lifetime.
    atr_at_entry: Mapped[float] = mapped_column(Float, nullable=False)
    initial_entry_price: Mapped[float] = mapped_column(Float, nullable=False)
    initial_stop_loss: Mapped[float] = mapped_column(Float, nullable=False)
    initial_take_profit: Mapped[float] = mapped_column(Float, nullable=False)

    # Mutable cohort-wide state
    weighted_avg_entry: Mapped[float] = mapped_column(Float, nullable=False)
    total_qty: Mapped[float] = mapped_column(Float, default=0.0)
    closed_qty: Mapped[float] = mapped_column(Float, default=0.0)  # cumulative scaled-out qty
    current_stop: Mapped[float] = mapped_column(Float, nullable=False)
    trail_high_water: Mapped[float] = mapped_column(Float, default=0.0)  # max favorable price seen
    max_favorable_r_seen: Mapped[float] = mapped_column(Float, default=0.0)  # high-water R-multiple after partial

    realized_pnl: Mapped[float] = mapped_column(Float, default=0.0)

    opened_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_action: Mapped[str | None] = mapped_column(String(64), nullable=True)
    forecast_drift: Mapped[float | None] = mapped_column(Float, nullable=True)
    forecast_confidence: Mapped[float | None] = mapped_column(Float, nullable=True)

    legs: Mapped[list["CohortLeg"]] = relationship(back_populates="cohort", cascade="all, delete-orphan")


class CohortLeg(Base):
    """One leg of a Cohort — a single TradeLocker position.

    Each leg has its own broker position id (since hedging mode opens a new
    position per order). The cohort logic computes weighted averages across
    these legs.
    """

    __tablename__ = "cohort_legs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    cohort_id: Mapped[int] = mapped_column(ForeignKey("cohorts.id"), index=True, nullable=False)
    role: Mapped[str] = mapped_column(String(16), nullable=False)  # entry|scale_in|hedge_close
    side: Mapped[str] = mapped_column(String(8), nullable=False)
    entry_price: Mapped[float] = mapped_column(Float, nullable=False)
    qty: Mapped[float] = mapped_column(Float, nullable=False)
    stop_loss: Mapped[float | None] = mapped_column(Float, nullable=True)
    take_profit: Mapped[float | None] = mapped_column(Float, nullable=True)

    tradelocker_position_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    tradelocker_order_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

    is_open: Mapped[bool] = mapped_column(Boolean, default=True)
    closed_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    pnl_usd: Mapped[float] = mapped_column(Float, default=0.0)

    opened_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    cohort: Mapped["Cohort"] = relationship(back_populates="legs")


class StrategyTickLog(Base):
    """Per-tick decision log for the strategy runner.

    Every time the runner evaluates a (bot, timeframe, symbol) it writes
    one row capturing what it saw and what it decided — entry, skip,
    scale-in, partial-close, trail, exit.

    The /strategy dashboard renders these live so users can see the bot's
    "thinking" — including SKIPs (the most informative case: "BTC
    confidence 0.42 was below threshold 0.80, no entry").

    Auto-archived: a startup cleanup keeps only the most recent N rows
    per (bot, timeframe) to bound disk usage.
    """

    __tablename__ = "strategy_tick_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    bot_id: Mapped[int] = mapped_column(ForeignKey("bots.id"), index=True, nullable=False)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True, index=True)
    timeframe: Mapped[str] = mapped_column(String(8), nullable=False)
    tick_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)

    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    current_price: Mapped[float | None] = mapped_column(Float, nullable=True)

    forecast_drift: Mapped[float | None] = mapped_column(Float, nullable=True)
    forecast_std: Mapped[float | None] = mapped_column(Float, nullable=True)
    forecast_confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    threshold: Mapped[float | None] = mapped_column(Float, nullable=True)

    # Decision discriminator. Values: "entry_long" | "entry_short" |
    # "skip_below_threshold" | "skip_existing_position" | "skip_paused" |
    # "scale_in" | "partial_close" | "trail_sl" | "exit" | "error"
    decision: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    extra: Mapped[str] = mapped_column(Text, default="{}")


class BrokerStatement(Base):
    """Periodic snapshot of broker truth per TradingAccount.

    Immutable. Each row captures the canonical state of an account as the
    broker reported it at ``pulled_at``: balance, open positions, working
    orders. Raw JSON is preserved verbatim so partners can recompute any
    derived metric from the broker's own bytes, plus a sha256 content
    hash so any subsequent rewrite would be detectable.

    The reconciliation diff endpoint compares these snapshots against our
    SlippageRecord rows to surface drift (missing close response, ghost
    positions, P&L mismatches). Snapshots feed the partner dashboard's
    "broker history" tab and the Task #8 cron's daily reconciliation
    report.
    """

    __tablename__ = "broker_statements"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    owner_user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id"), nullable=False, index=True
    )
    # Optional FK — preserved across TradingAccount deletion so the
    # historical snapshot remains queryable by broker-side IDs alone.
    trading_account_id: Mapped[int | None] = mapped_column(
        ForeignKey("trading_accounts.id"), nullable=True, index=True
    )
    tradelocker_account_id: Mapped[str] = mapped_column(
        String(64), nullable=False, index=True
    )
    tradelocker_acc_num: Mapped[str] = mapped_column(String(16), nullable=False)
    tradelocker_env: Mapped[str] = mapped_column(String(8), nullable=False)

    pulled_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, index=True
    )

    # Derived totals — convenient for time-series queries without
    # unpacking the raw JSON on every read.
    balance: Mapped[float | None] = mapped_column(Float, nullable=True)
    equity: Mapped[float | None] = mapped_column(Float, nullable=True)
    open_pnl: Mapped[float | None] = mapped_column(Float, nullable=True)
    positions_count: Mapped[int] = mapped_column(Integer, default=0)
    orders_count: Mapped[int] = mapped_column(Integer, default=0)

    # Raw broker JSON — the trust anchor.
    raw_account_state_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    raw_positions_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    raw_orders_json: Mapped[str | None] = mapped_column(Text, nullable=True)

    # sha256(state || positions || orders) — any change rehashes differently
    # so subsequent edits to this row are detectable.
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)


class SlippageRecord(Base):
    """Per-trade audit record comparing strategy expectation vs live broker
    fill, written by the slippage tracker at three lifecycle points:

      1. Entry signal emitted by strategy → status=pending, expected_*
         fields populated, signal_ts captured.
      2. Broker confirms order ack/fill → actual_entry_price + entry
         slippage + latency fields populated, status=open.
      3. Position closes → exit_type + actual_exit_price + exit slippage
         + P&L fields populated, status=closed.

    This is the foundational table the partner dashboard, the 2-week
    audit, and the slippage stress test all read from. Backtest assumes
    a certain entry/exit slippage; this table records what actually
    happened so the comparison is objective.

    Indexed on (user_id, strategy_name, created_at) for the partner
    dashboard's per-strategy time-range queries.
    """

    __tablename__ = "slippage_records"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    # Trade identity
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id"), nullable=False, index=True
    )
    strategy_name: Mapped[str] = mapped_column(
        String(64), nullable=False, index=True
    )
    # The TradeLocker sub-account the trade was placed on. Not an FK because
    # this string is broker-side and may not match any local Account row.
    account_id: Mapped[str] = mapped_column(String(64), nullable=False)
    # Optional link back to the local Execution row (when one exists).
    execution_id: Mapped[int | None] = mapped_column(
        ForeignKey("executions.id"), nullable=True, index=True
    )
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    side: Mapped[str] = mapped_column(String(8), nullable=False)  # "buy" | "sell"

    # Status — drives the partner dashboard's "open trades" filter and the
    # reconciliation diff. Values: "pending" | "open" | "closed" | "rejected".
    status: Mapped[str] = mapped_column(
        String(16), default="pending", nullable=False, index=True
    )

    # Timing (UTC, ISO microsecond precision). bar_close_ts and signal_ts
    # are required at signal emit; the rest fill in as the lifecycle proceeds.
    bar_close_ts: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    signal_ts: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    order_submit_ts: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    order_ack_ts: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    fill_ts: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    closed_ts: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    # Entry — expected fields are set at signal emit; actual_entry_price is
    # set when the broker confirms the fill.
    bar_close_price: Mapped[float] = mapped_column(Float, nullable=False)
    expected_entry_price: Mapped[float] = mapped_column(Float, nullable=False)
    actual_entry_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    # Signed in points: positive = filled WORSE than expected (long fills
    # higher, short fills lower than the model predicted).
    entry_slippage_pts: Mapped[float | None] = mapped_column(Float, nullable=True)

    # Stop / exit expectations published by the strategy at signal time.
    # Used by the audit to compute where the trade SHOULD have closed and
    # compare against what actually happened.
    hard_stop_distance_pts: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    trailing_stop_distance_pts: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    early_stop_condition: Mapped[str] = mapped_column(String(64), default="", nullable=False)

    # Exit — populated when the trade closes.
    # exit_type values: "hard_stop" | "early_stop" | "trailing" | "manual" |
    # "take_profit" | "reconciliation_close" (forced by drift detection)
    exit_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    peak_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    expected_exit_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    actual_exit_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    exit_slippage_pts: Mapped[float | None] = mapped_column(Float, nullable=True)

    # P&L — both the backtest-projection P&L (what the strategy SHOULD have
    # made if its expected fills were honored) and the real P&L the broker
    # actually delivered. Delta = edge erosion to friction.
    strategy_pnl_pts: Mapped[float | None] = mapped_column(Float, nullable=True)
    real_pnl_pts: Mapped[float | None] = mapped_column(Float, nullable=True)
    slippage_total_pts: Mapped[float | None] = mapped_column(Float, nullable=True)
    slippage_total_dollars: Mapped[float | None] = mapped_column(Float, nullable=True)

    # Latency (ms from bar_close_ts). Pinpoints where execution time is
    # being lost — strategy computation, network egress, or broker fill.
    signal_latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    submit_latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    broker_ack_latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    fill_latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    total_latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # Raw broker JSON response — the trust anchor. Partner dashboards expose
    # this verbatim so a developer can independently parse and verify our
    # computed slippage numbers against the broker's own record.
    broker_fill_response_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    broker_close_response_json: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Free-form metadata published by the strategy (velocity score, RSI
    # snapshot, whatever the strategy author wants to capture).
    extra_json: Mapped[str] = mapped_column(Text, default="{}", nullable=False)

    # Bookkeeping
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, index=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )
