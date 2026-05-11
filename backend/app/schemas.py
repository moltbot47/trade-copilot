"""Pydantic v2 schemas for request and response payloads."""
from __future__ import annotations

from datetime import datetime
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator


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
    risk_appetite: str = "balanced"
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
    # Optional subset of the bot's instruments. None or empty list = subscribe
    # to all instruments the bot trades (legacy / default behavior).
    allowed_instruments: Optional[list[str]] = None


class SubscriptionUpdate(BaseModel):
    aggression_level: Optional[int] = Field(default=None, ge=1, le=10)
    is_paused: Optional[bool] = None
    # Setting to None leaves the existing filter unchanged. Pass an empty list
    # to reset to "all instruments." Pass a non-empty list to narrow.
    allowed_instruments: Optional[list[str]] = None


class SubscriptionOut(BaseModel):
    # NOTE: not from_attributes — the API endpoint builds this dict explicitly
    # because the ORM stores allowed_instruments as CSV and we expose it as
    # list[str] | None on the wire.
    id: int
    user_id: int
    bot_id: int
    aggression_level: int
    is_paused: bool
    allowed_instruments: Optional[list[str]] = None
    created_at: datetime
    updated_at: datetime


# ---------- TradeLocker ----------
class TradeLockerConnect(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=128)
    server: str = Field(min_length=1, max_length=32, pattern=r"^[A-Za-z0-9_-]+$")
    env: Literal["demo", "live"] = "demo"
    # Optional account picker — TradeLocker logins often have multiple
    # accounts (demo + live + prop). If omitted, the connect endpoint will
    # either (a) auto-link the only account, or (b) refuse with a 409
    # carrying the candidate list so the frontend can prompt.
    account_id: Optional[str] = None

    # Strip whitespace BEFORE the field-level pattern check runs. Without
    # this, a user copy-pasting "GENFX " (trailing space) produced a 422
    # with no useful error because the regex `^[A-Za-z0-9_-]+$` rejects
    # whitespace anywhere — including padding. mode="before" runs ahead of
    # the pattern check; an empty stripped value still hits min_length=1
    # and surfaces a more accurate "must be non-empty" message.
    @field_validator("server", "password", mode="before")
    @classmethod
    def _strip_whitespace(cls, v: object) -> object:
        if isinstance(v, str):
            return v.strip()
        return v


class TradeLockerAccountListItem(BaseModel):
    """One row in the account picker — slim subset of the broker's response."""
    account_id: str
    acc_num: str
    name: Optional[str] = None
    balance: float = 0.0
    currency: Optional[str] = None
    # TradeLocker exposes a 'status' enum (ACTIVE, ARCHIVED, ...) — we pass it
    # through so the UI can grey out non-tradeable rows.
    status: Optional[str] = None
    # Broker-side type hint (LIVE / DEMO / PROP / etc.) where TradeLocker
    # provides one; otherwise None and the UI falls back to env.
    type: Optional[str] = None
    # True if this is the account currently linked on the user's session —
    # the picker UI uses this to mark the active row.
    is_current: bool = False


class TradeLockerSwitchAccount(BaseModel):
    """Body for POST /api/tradelocker/switch-account."""
    account_id: str


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
