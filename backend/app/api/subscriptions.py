"""User subscription management."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.api.users import get_current_user
from app.db.database import get_db
from app.db.models import Bot, Subscription, User
from app.schemas import StatusResponse, SubscriptionCreate, SubscriptionOut, SubscriptionUpdate

router = APIRouter(prefix="/subscriptions", tags=["subscriptions"])


@router.get("", response_model=list[SubscriptionOut])
def list_my_subscriptions(
    user: User = Depends(get_current_user), db: Session = Depends(get_db)
) -> list[SubscriptionOut]:
    subs = db.query(Subscription).filter(Subscription.user_id == user.id).all()
    return [SubscriptionOut.model_validate(s) for s in subs]


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
    sub = Subscription(
        user_id=user.id, bot_id=payload.bot_id, aggression_level=payload.aggression_level
    )
    db.add(sub)
    db.commit()
    db.refresh(sub)
    return SubscriptionOut.model_validate(sub)


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
    db.commit()
    db.refresh(sub)
    return SubscriptionOut.model_validate(sub)


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
