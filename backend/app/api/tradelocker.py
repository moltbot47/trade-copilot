"""TradeLocker connect + account info endpoints."""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from slowapi.util import get_remote_address
from sqlalchemy.orm import Session

from app.api.users import get_current_user
from app.core.crypto import decrypt, encrypt
from app.core.rate_limit import limiter
from app.core.tradelocker_client import TradeLockerClient, TradeLockerError
from app.db.database import get_db
from app.db.models import User
from app.schemas import StatusResponse, TradeLockerAccountOut, TradeLockerConnect

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/tradelocker", tags=["tradelocker"])


def _connect_key(request: Request) -> str:
    """Rate-limit key: (IP, authenticated-user-email if available).

    Falls back to IP-only when no session is attached. The user's email
    is stashed onto request.state by the get_current_user dependency
    chain at route-execution time; slowapi calls this AFTER deps run
    when used as a route decorator.
    """
    ip = get_remote_address(request)
    email = getattr(request.state, "auth_email", None)
    return f"{ip}|{email}" if email else ip


@router.post("/connect", response_model=StatusResponse)
@limiter.limit("5/minute", key_func=_connect_key)
async def connect(
    request: Request,
    payload: TradeLockerConnect,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> StatusResponse:
    # Stash for the rate-limit key (effective on the NEXT request from
    # this user; first hit keys on IP-only, which is also fine).
    request.state.auth_email = user.email
    client = TradeLockerClient(env=payload.env)
    try:
        result = await client.authenticate(payload.email, payload.password, payload.server)
    except TradeLockerError as exc:
        # Log full detail server-side; surface a clean message to the user.
        logger.warning("tradelocker auth failed for %s: %s", payload.email, exc)
        msg = str(exc).lower()
        if "incorrect email or password" in msg:
            user_msg = "Invalid email, password, or server."
        elif "server exists" in msg or "failed to fetch token" in msg:
            user_msg = "Server name not recognized. Genesis FX uses 'GENFX'."
        elif "network" in msg or "timeout" in msg:
            user_msg = "Could not reach TradeLocker. Try again."
        else:
            user_msg = "Connection failed. Verify your credentials and server."
        raise HTTPException(status_code=400, detail=user_msg)

    user.tradelocker_email = encrypt(payload.email)
    user.tradelocker_token = encrypt(result.get("access_token"))
    user.tradelocker_refresh_token = encrypt(result.get("refresh_token"))
    user.tradelocker_account_id = result.get("account_id")
    user.tradelocker_acc_num = result.get("acc_num") or "1"
    user.tradelocker_server = payload.server
    user.tradelocker_env = payload.env
    db.commit()
    return StatusResponse(status="connected", detail=user.tradelocker_account_id or "")


@router.get("/account", response_model=TradeLockerAccountOut)
async def account_state(user: User = Depends(get_current_user)) -> TradeLockerAccountOut:
    if not user.tradelocker_token or not user.tradelocker_account_id:
        return TradeLockerAccountOut(connected=False, env=user.tradelocker_env or "demo")
    token = decrypt(user.tradelocker_token)
    if not token:
        return TradeLockerAccountOut(connected=False, env=user.tradelocker_env or "demo")
    client = TradeLockerClient(env=user.tradelocker_env or "demo")
    try:
        state = await client.get_account_state(
            user.tradelocker_account_id, token, user.tradelocker_acc_num or "1"
        )
    except TradeLockerError as exc:
        logger.info("tradelocker state lookup failed: %s", exc)
        return TradeLockerAccountOut(
            connected=True,
            env=user.tradelocker_env or "demo",
            account_id=user.tradelocker_account_id,
            acc_num=user.tradelocker_acc_num,
            server=user.tradelocker_server,
        )

    if not isinstance(state, dict):
        state = {}

    return TradeLockerAccountOut(
        connected=True,
        env=user.tradelocker_env or "demo",
        account_id=user.tradelocker_account_id,
        acc_num=user.tradelocker_acc_num,
        server=user.tradelocker_server,
        balance=state.get("balance"),
        # TradeLocker doesn't expose a single "equity" — projectedBalance is
        # closest (= cash + unrealized PnL).
        equity=state.get("projectedBalance") or state.get("balance"),
        available_funds=state.get("availableFunds"),
        open_pnl=state.get("openGrossPnL"),
        today_net=state.get("todayNet"),
        positions_count=int(state.get("positionsCount") or 0),
        currency="USD",
    )
