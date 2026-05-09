"""SQLAlchemy ORM models for Trade Copilot."""
from __future__ import annotations

import enum
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


class StrategyType(str, enum.Enum):
    orb = "orb"
    squeeze = "squeeze"
    stoch_hook = "stoch_hook"
    latpfn_momentum = "latpfn_momentum"


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


class TradeOutcome(Base):
    """Closed trade record for the feedback loop."""

    __tablename__ = "trade_outcomes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    bot_id: Mapped[int] = mapped_column(ForeignKey("bots.id"), index=True, nullable=False)
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
