"""Pydantic v2 schemas for request and response payloads."""
from __future__ import annotations

from datetime import datetime
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, EmailStr, Field


# ---------- Users ----------
class UserCreate(BaseModel):
    email: EmailStr
    password: str = Field(min_length=6, max_length=128)


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    email: EmailStr
    created_at: datetime
    is_active: bool
    max_daily_loss_pct: float
    tradelocker_account_id: Optional[str] = None
    tradelocker_env: str = "demo"


# ---------- Bots ----------
class BotOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    name: str
    slug: str
    description: str
    strategy_type: str
    backtest_win_rate: float
    backtest_profit_factor: float
    risk_level: int
    instruments_csv: str
    is_active: bool
    created_at: datetime


# ---------- Subscriptions ----------
class SubscriptionCreate(BaseModel):
    bot_id: int
    aggression_level: int = Field(default=5, ge=1, le=10)


class SubscriptionUpdate(BaseModel):
    aggression_level: Optional[int] = Field(default=None, ge=1, le=10)
    is_paused: Optional[bool] = None


class SubscriptionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    user_id: int
    bot_id: int
    aggression_level: int
    is_paused: bool
    created_at: datetime
    updated_at: datetime


# ---------- TradeLocker ----------
class TradeLockerConnect(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=128)
    server: str = Field(min_length=1, max_length=32, pattern=r"^[A-Za-z0-9_-]+$")
    env: Literal["demo", "live"] = "demo"


class TradeLockerAccountOut(BaseModel):
    account_id: Optional[str] = None
    acc_num: Optional[str] = None
    server: Optional[str] = None
    env: str = "demo"
    balance: Optional[float] = None
    equity: Optional[float] = None  # = projectedBalance (incl. unrealized)
    available_funds: Optional[float] = None
    open_pnl: Optional[float] = None  # = openGrossPnL
    today_net: Optional[float] = None
    positions_count: Optional[int] = None
    currency: Optional[str] = "USD"
    connected: bool = False


# ---------- Webhooks ----------
class TradingViewSignal(BaseModel):
    # `bot_secret` is now OPTIONAL: kept for the deprecated body-secret path
    # (see app/api/webhooks.py). Hardened mode supplies the bot identity via
    # the `X-Bot-Slug` header instead, with HMAC signature in `X-Webhook-Signature`.
    bot_secret: Optional[str] = None
    instrument: str
    side: Literal["buy", "sell"]
    entry_price: Optional[float] = None
    stop_loss: Optional[float] = None
    take_profit: Optional[float] = None
    base_lot_size: float = 0.01

    # Allow passthrough of any extra TradingView fields
    model_config = ConfigDict(extra="allow")


class WebhookResponse(BaseModel):
    status: str
    signal_id: int
    subscribers_notified: int


class BotWebhookSecretOut(BaseModel):
    """Returned only to authenticated owners. Carries the live secret."""

    slug: str
    webhook_url: str
    secret: str
    signature_header_format: str = (
        "X-Bot-Slug: <slug>; X-Webhook-Timestamp: <unix-seconds>; "
        "X-Webhook-Signature: hex(HMAC_SHA256(secret, f'{ts}.{body}'))"
    )
    max_age_seconds: int = 300


# ---------- Dashboard ----------
class PnLSummary(BaseModel):
    total_executions: int
    filled: int
    rejected: int
    errors: int
    pending: int
    realized_pnl: float = 0.0


class PositionOut(BaseModel):
    instrument: Optional[str] = None
    side: Optional[str] = None
    quantity: Optional[float] = None
    avg_price: Optional[float] = None
    unrealized_pnl: Optional[float] = None
    raw: Optional[dict[str, Any]] = None


# ---------- Generic ----------
class StatusResponse(BaseModel):
    status: str
    detail: Optional[str] = None
