"""User subscription management."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.api.users import get_current_user
from app.db.database import get_db
from app.db.models import Bot, Subscription, User
from app.schemas import StatusResponse, SubscriptionCreate, SubscriptionOut, SubscriptionUpdate

router = APIRouter(prefix="/subscriptions", tags=["subscriptions"])


def _bot_instruments(bot: Bot) -> list[str]:
    """Parse Bot.instruments_csv → list of canonical symbols (uppercased, deduped)."""
    raw = bot.instruments_csv or ""
    seen: list[str] = []
    for token in raw.split(","):
        sym = token.strip().upper()
        if sym and sym not in seen:
            seen.append(sym)
    return seen


def _validate_and_normalize_instruments(
    requested: list[str] | None, bot: Bot
) -> str | None:
    """Validate the user's instrument selection against the bot's catalog.

    Returns the CSV to persist (or None for "all instruments"). Raises 400
    if the user passes a symbol the bot doesn't trade — without this guard
    a user could silently subscribe to MSFT under a forex bot and never see
    a single signal, with no way to tell why.
    """
    if requested is None:
        return None
    # Empty list also means "all instruments" — same as None.
    if len(requested) == 0:
        return None

    bot_instruments = _bot_instruments(bot)
    if not bot_instruments:
        # Bot has no declared instruments; can't validate. Accept as-is.
        return ",".join(sorted({s.strip().upper() for s in requested if s.strip()}))

    bot_set = set(bot_instruments)
    normalized = [s.strip().upper() for s in requested if s.strip()]
    if not normalized:
        return None
    unknown = [s for s in normalized if s not in bot_set]
    if unknown:
        raise HTTPException(
            status_code=400,
            detail=(
                f"instruments not offered by this bot: {','.join(unknown)}. "
                f"Bot trades: {','.join(bot_instruments)}."
            ),
        )
    # Dedupe while preserving the bot's canonical order.
    keep = [sym for sym in bot_instruments if sym in set(normalized)]
    return ",".join(keep)


def _to_out(sub: Subscription) -> SubscriptionOut:
    """Build the API response, splitting the CSV column back into a list."""
    raw = sub.allowed_instruments
    parsed: list[str] | None
    if raw:
        parsed = [s.strip() for s in raw.split(",") if s.strip()] or None
    else:
        parsed = None
    return SubscriptionOut(
        id=sub.id,
        user_id=sub.user_id,
        bot_id=sub.bot_id,
        aggression_level=sub.aggression_level,
        is_paused=sub.is_paused,
        allowed_instruments=parsed,
        created_at=sub.created_at,
        updated_at=sub.updated_at,
    )


@router.get("", response_model=list[SubscriptionOut])
def list_my_subscriptions(
    user: User = Depends(get_current_user), db: Session = Depends(get_db)
) -> list[SubscriptionOut]:
    subs = db.query(Subscription).filter(Subscription.user_id == user.id).all()
    return [_to_out(s) for s in subs]


@router.post("", response_model=SubscriptionOut, status_code=status.HTTP_201_CREATED)
def create_subscription(
    payload: SubscriptionCreate,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> SubscriptionOut:
    bot = db.get(Bot, payload.bot_id)
    if bot is None:
        raise HTTPException(status_code=404, detail="bot not found")
    existing = (
        db.query(Subscription)
        .filter(Subscription.user_id == user.id, Subscription.bot_id == payload.bot_id)
        .first()
    )
    if existing:
        raise HTTPException(status_code=409, detail="already subscribed")
    allowed_csv = _validate_and_normalize_instruments(payload.allowed_instruments, bot)
    sub = Subscription(
        user_id=user.id,
        bot_id=payload.bot_id,
        aggression_level=payload.aggression_level,
        allowed_instruments=allowed_csv,
    )
    db.add(sub)
    db.commit()
    db.refresh(sub)
    return _to_out(sub)


@router.patch("/{sub_id}", response_model=SubscriptionOut)
def update_subscription(
    sub_id: int,
    payload: SubscriptionUpdate,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> SubscriptionOut:
    sub = db.get(Subscription, sub_id)
    if sub is None or sub.user_id != user.id:
        raise HTTPException(status_code=404, detail="subscription not found")
    if payload.aggression_level is not None:
        sub.aggression_level = payload.aggression_level
    if payload.is_paused is not None:
        sub.is_paused = payload.is_paused
    if payload.allowed_instruments is not None:
        bot = db.get(Bot, sub.bot_id)
        # bot must still exist; we don't soft-delete bots in this codebase
        assert bot is not None
        sub.allowed_instruments = _validate_and_normalize_instruments(
            payload.allowed_instruments, bot
        )
    db.commit()
    db.refresh(sub)
    return _to_out(sub)


@router.delete("/{sub_id}", response_model=StatusResponse)
def delete_subscription(
    sub_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> StatusResponse:
    sub = db.get(Subscription, sub_id)
    if sub is None or sub.user_id != user.id:
        raise HTTPException(status_code=404, detail="subscription not found")
    db.delete(sub)
    db.commit()
    return StatusResponse(status="deleted")
