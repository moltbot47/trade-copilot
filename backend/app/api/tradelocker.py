"""TradeLocker connect + account info endpoints."""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request, Response
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
    response: Response,  # required by slowapi to inject X-RateLimit-* headers
    payload: TradeLockerConnect,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> StatusResponse:
    """Idempotent connect — running it twice with same or different creds is safe.

    Flow:
      1. Authenticate against TradeLocker.
      2. If user already has a relay running, stop it.
      3. Save new creds (encrypted).
      4. Start fresh relay.
    Errors are caught and surfaced as 4xx with friendly messages. The
    global 500 handler should not fire for any expected condition here.
    """
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

    access_token = result.get("access_token")
    if not access_token:
        # Should never happen — TradeLockerClient.authenticate raises if it
        # can't find a token. Defensive 502 because it's an upstream issue.
        logger.error("tradelocker authenticate returned no access_token: %s", result)
        raise HTTPException(status_code=502, detail="Broker auth returned no token.")

    # ----- Account selection -----
    # TradeLocker logins commonly carry multiple accounts (demo + live + prop).
    # Previously we silently picked index 0, which is why a live account
    # could connect under a different account id than the user expected.
    # Now: if the user passed an explicit account_id, honor it; otherwise
    # auto-link only when there's a single account; refuse with 409 + the
    # candidate list when there's more than one and the user hasn't chosen.
    all_accounts: list = result.get("all_accounts") or []
    chosen: dict | None = None
    if payload.account_id:
        chosen = next(
            (a for a in all_accounts if str(a.get("id")) == str(payload.account_id)),
            None,
        )
        if chosen is None:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"account_id {payload.account_id!r} not found among your "
                    f"{len(all_accounts)} linked TradeLocker accounts."
                ),
            )
    elif len(all_accounts) == 1:
        chosen = all_accounts[0]
    else:
        # Tell the frontend to prompt. We don't persist any credentials in
        # this branch — the auth_token would be useful but it's session-only
        # so the cost of asking the user to re-authenticate after picking
        # is one extra POST. Worth the security floor.
        candidates = [
            {
                "account_id": str(a.get("id")),
                "acc_num": str(a.get("accNum")),
                "name": a.get("name"),
                "balance": float(a.get("accountBalance", 0) or 0),
                "currency": a.get("currency"),
                "status": a.get("status"),
                "type": a.get("accountType"),
            }
            for a in all_accounts
        ]
        raise HTTPException(
            status_code=409,
            detail={
                "code": "select_account",
                "message": (
                    f"You have {len(all_accounts)} linked TradeLocker accounts. "
                    f"Resubmit with one of the account_id values from `accounts`."
                ),
                "accounts": candidates,
            },
        )

    # Stop any existing relay BEFORE we swap credentials so it doesn't
    # keep using stale tokens. Idempotent — no-op if no relay is running.
    # Snapshot "was this a reconnect?" first — used for the admin alert
    # so we can distinguish first connect from reconnect-with-same-broker.
    was_already_connected = bool(user.tradelocker_account_id)
    try:
        from app.ws.relay_manager import relay_manager

        await relay_manager.stop_for_user(user.id)
    except Exception as exc:  # pragma: no cover
        logger.debug("relay stop on reconnect failed (non-fatal): %s", exc)

    # Save new creds. Wrap in a fine-grained try so we don't 500 on a
    # transient DB or encryption failure.
    try:
        user.tradelocker_email = encrypt(payload.email)
        user.tradelocker_token = encrypt(access_token)
        user.tradelocker_refresh_token = (
            encrypt(result.get("refresh_token")) if result.get("refresh_token") else None
        )
        user.tradelocker_account_id = str(chosen.get("id"))
        user.tradelocker_acc_num = str(chosen.get("accNum") or "1")
        user.tradelocker_server = payload.server
        user.tradelocker_env = payload.env
        db.commit()
    except Exception as exc:
        db.rollback()
        logger.exception("connect: failed to persist credentials: %s", exc)
        raise HTTPException(status_code=500, detail="Could not save credentials. Try again.")

    # Start the new relay. Fire-and-forget; never block the response.
    try:
        from app.ws.relay_manager import relay_manager

        relay_manager.start_for_user(user.id)
    except Exception as exc:  # pragma: no cover
        logger.debug("relay_manager.start_for_user skipped: %s", exc)

    # Admin Discord alert — distinguishes first connect from reconnect so
    # the operator can react to genuinely new users without being spammed.
    try:
        from app.integrations.discord_signals import post_admin_event_fire_and_forget

        post_admin_event_fire_and_forget(
            event="broker_connect" if not was_already_connected else "broker_reconnect",
            user_email=user.email,
            details={
                "broker": "Genesis FX",
                "env": user.tradelocker_env or "demo",
                "account_id": user.tradelocker_account_id or "?",
            },
        )
    except Exception as exc:  # noqa: BLE001 — never block connect on Discord
        logger.debug("admin alert (broker_connect) skipped: %s", exc)

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


@router.get("/positions")
async def list_positions(
    user: User = Depends(get_current_user),
) -> dict:
    """List open positions on the linked broker account.

    Returns broker-truth, including the resolved symbol name (via the
    /instruments lookup) and SL/TP IDs so the UI can show "no SL set"
    in red when those are missing. Used by the dashboard's modify-SL/TP
    modal.
    """
    if not user.tradelocker_token or not user.tradelocker_account_id:
        return {"positions": []}
    token = decrypt(user.tradelocker_token)
    if not token:
        return {"positions": []}

    client = TradeLockerClient(env=user.tradelocker_env or "demo")
    try:
        positions = await client.get_positions(
            user.tradelocker_account_id, token, user.tradelocker_acc_num or "1"
        )
    except TradeLockerError as exc:
        logger.info("list_positions broker error: %s", exc)
        return {"positions": []}

    # Resolve tradableInstrumentId → symbol via one /instruments call.
    try:
        inst = await client.get_instruments(
            user.tradelocker_account_id, token, user.tradelocker_acc_num or "1"
        )
        tid_to_name = {i.get("tradableInstrumentId"): i.get("name") for i in inst}
    except TradeLockerError:
        tid_to_name = {}

    return {
        "positions": [
            {
                "id": str(p.get("id")),
                "symbol": tid_to_name.get(p.get("tradableInstrumentId"), f"tid#{p.get('tradableInstrumentId')}"),
                "side": p.get("side"),
                "qty": float(p.get("qty") or 0),
                "avg_price": float(p.get("avgPrice") or 0),
                "unrealized_pl": float(p.get("unrealizedPl") or 0),
                "has_sl": bool(p.get("stopLossId")),
                "has_tp": bool(p.get("takeProfitId")),
                "stop_loss_id": str(p.get("stopLossId") or "") or None,
                "take_profit_id": str(p.get("takeProfitId") or "") or None,
            }
            for p in positions
        ],
    }


@router.post("/positions/{position_id}/modify", response_model=StatusResponse)
async def modify_position_levels(
    position_id: str,
    payload: dict,
    user: User = Depends(get_current_user),
) -> StatusResponse:
    """Set/update stop loss and/or take profit on an open broker position.

    Wraps TradeLockerClient.modify_position. At least one of `stop_loss`
    or `take_profit` is required. Broker accepts a price; we pass float.
    Returns {"status": "ok"} on success. Errors are surfaced as HTTP 400
    so the UI can show "broker rejected: too close to current price" etc.
    """
    if not user.tradelocker_token or not user.tradelocker_account_id:
        raise HTTPException(status_code=400, detail="no broker linked")
    token = decrypt(user.tradelocker_token)
    if not token:
        raise HTTPException(status_code=400, detail="broker token unreadable")

    sl = payload.get("stop_loss")
    tp = payload.get("take_profit")
    if sl is None and tp is None:
        raise HTTPException(status_code=400, detail="stop_loss or take_profit is required")

    client = TradeLockerClient(env=user.tradelocker_env or "demo")
    try:
        await client.modify_position(
            position_id=position_id,
            token=token,
            acc_num=user.tradelocker_acc_num or "1",
            stop_loss=float(sl) if sl is not None else None,
            take_profit=float(tp) if tp is not None else None,
        )
    except TradeLockerError as exc:
        logger.info("modify_position(%s) broker error: %s", position_id, exc)
        raise HTTPException(status_code=400, detail=f"broker rejected: {exc}")

    return StatusResponse(status="ok", detail=position_id)


# ---------------------------------------------------------------------
# Account switcher — list + swap without re-typing credentials
# ---------------------------------------------------------------------

@router.get("/accounts")
async def list_accounts(
    user: User = Depends(get_current_user),
) -> dict:
    """List every TradeLocker account available to the linked session.

    Uses the user's saved access token (no password round-trip). If the
    token has expired the broker call returns 401 and the user has to
    /connect again — same recovery path as everywhere else.

    Response shape mirrors the 409 picker payload from /connect so the
    frontend can reuse the same component.
    """
    if not user.tradelocker_token:
        raise HTTPException(status_code=400, detail="no broker linked")
    token = decrypt(user.tradelocker_token)
    if not token:
        raise HTTPException(status_code=400, detail="broker token unreadable")

    client = TradeLockerClient(env=user.tradelocker_env or "demo")
    try:
        accounts = await client.list_all_accounts(token)
    except TradeLockerError as exc:
        logger.info("list_accounts upstream error: %s", exc)
        raise HTTPException(
            status_code=502,
            detail="Broker is not responding. Try /connect again to refresh your session.",
        )

    current_id = str(user.tradelocker_account_id or "")
    return {
        "current_account_id": current_id or None,
        "env": user.tradelocker_env or "demo",
        "accounts": [
            {
                "account_id": str(a.get("id")),
                "acc_num": str(a.get("accNum")),
                "name": a.get("name"),
                "balance": float(a.get("accountBalance", 0) or 0),
                "currency": a.get("currency"),
                "status": a.get("status"),
                "type": a.get("accountType"),
                "is_current": str(a.get("id")) == current_id,
            }
            for a in accounts
        ],
    }


@router.post("/switch-account", response_model=StatusResponse)
async def switch_account(
    payload: dict,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> StatusResponse:
    """Swap the linked account to a different one from the same session.

    No re-auth required — uses the saved access token to refresh the
    account list, validates that `payload.account_id` is in the list,
    then updates the User row + restarts the relay.
    """
    target_account_id = str(payload.get("account_id", "")).strip()
    if not target_account_id:
        raise HTTPException(status_code=400, detail="account_id is required")

    if not user.tradelocker_token:
        raise HTTPException(status_code=400, detail="no broker linked")
    token = decrypt(user.tradelocker_token)
    if not token:
        raise HTTPException(status_code=400, detail="broker token unreadable")

    client = TradeLockerClient(env=user.tradelocker_env or "demo")
    try:
        accounts = await client.list_all_accounts(token)
    except TradeLockerError as exc:
        logger.info("switch_account upstream error: %s", exc)
        raise HTTPException(
            status_code=502,
            detail="Broker is not responding. Try /connect again to refresh.",
        )

    chosen = next((a for a in accounts if str(a.get("id")) == target_account_id), None)
    if chosen is None:
        raise HTTPException(
            status_code=400,
            detail=(
                f"account_id {target_account_id!r} is not in your TradeLocker "
                f"account list. Refresh and try again."
            ),
        )

    # Stop the existing relay before the swap so it doesn't keep pulling
    # state from the old account_id.
    try:
        from app.ws.relay_manager import relay_manager
        await relay_manager.stop_for_user(user.id)
    except Exception as exc:  # pragma: no cover
        logger.debug("relay stop on switch_account failed (non-fatal): %s", exc)

    try:
        user.tradelocker_account_id = str(chosen.get("id"))
        user.tradelocker_acc_num = str(chosen.get("accNum") or "1")
        db.commit()
    except Exception as exc:
        db.rollback()
        logger.exception("switch_account: persist failed: %s", exc)
        raise HTTPException(status_code=500, detail="Could not save the swap. Try again.")

    # Boot a relay against the new account.
    try:
        from app.ws.relay_manager import relay_manager
        relay_manager.start_for_user(user.id)
    except Exception as exc:  # pragma: no cover
        logger.debug("relay_manager.start_for_user skipped: %s", exc)

    # Admin Discord alert — useful for the operator to see when an
    # account swap happens.
    try:
        from app.integrations.discord_signals import post_admin_event_fire_and_forget
        post_admin_event_fire_and_forget(
            event="broker_reconnect",
            user_email=user.email,
            details={
                "broker": "Genesis FX",
                "env": user.tradelocker_env or "demo",
                "account_id": user.tradelocker_account_id or "?",
                "via": "switch_account",
            },
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug("admin alert (switch_account) skipped: %s", exc)

    return StatusResponse(status="switched", detail=user.tradelocker_account_id or "")


# ---------------------------------------------------------------------
# Tiny-account advisor
# ---------------------------------------------------------------------

@router.get("/advisor")
async def advisor(
    user: User = Depends(get_current_user),
    risk_appetite: str | None = None,
) -> dict:
    """Recommend instruments + strategy presets that fit the user's balance.

    Reads the user's live broker balance + the broker's instrument list,
    then maps them through ``tiny_account_advisor`` for the user's
    declared risk appetite (or the override passed as ``risk_appetite``).

    Tiny accounts ($5–$50) are the explicit target: the response tells
    the user which pairs they can actually open, how many they could hold
    concurrently, and which bot preset matches their appetite. The user
    is free to ignore it and subscribe to whatever they want.
    """
    from app.strategies.tiny_account_advisor import (
        PRESETS,
        compute_suggestions,
    )

    appetite = (risk_appetite or user.risk_appetite or "balanced").lower()
    if appetite not in PRESETS:
        appetite = "balanced"

    # Defensive: if broker isn't linked, return an empty-shaped response
    # rather than 404 — callers (e.g., the connect-success UI) prefer to
    # render an empty state over an error toast.
    if not user.tradelocker_token or not user.tradelocker_account_id:
        empty = compute_suggestions(0.0, appetite, [])  # type: ignore[arg-type]
        return _advisor_to_json(empty, connected=False)

    token = decrypt(user.tradelocker_token)
    if not token:
        empty = compute_suggestions(0.0, appetite, [])  # type: ignore[arg-type]
        return _advisor_to_json(empty, connected=False)

    client = TradeLockerClient(env=user.tradelocker_env or "demo")
    try:
        state = await client.get_account_state(
            user.tradelocker_account_id, token, user.tradelocker_acc_num or "1"
        )
    except TradeLockerError as exc:
        logger.info("advisor: account state lookup failed: %s", exc)
        state = {}

    balance = float(
        (state or {}).get("balance")
        or (state or {}).get("projectedBalance")
        or 0.0
    )

    # Pull the broker's instrument catalog. Cached per-process in the client.
    try:
        broker_instruments = await client.get_instruments(
            user.tradelocker_account_id, token, user.tradelocker_acc_num or "1"
        )
        names = [i.get("name") for i in broker_instruments if i.get("name")]
    except TradeLockerError as exc:
        logger.info("advisor: instruments lookup failed: %s", exc)
        names = []

    result = compute_suggestions(balance, appetite, names)  # type: ignore[arg-type]
    return _advisor_to_json(result, connected=True)


def _advisor_to_json(result, *, connected: bool) -> dict:
    """Flatten the dataclass response into a JSON-friendly dict."""
    preset = result.preset
    return {
        "connected": connected,
        "balance_usd": result.balance_usd,
        "risk_appetite": result.risk_appetite,
        "preset": {
            "label": preset.label,
            "description": preset.description,
            "confidence_threshold": preset.confidence_threshold,
            "target_rr": preset.target_rr,
            "max_margin_pct_per_pair": preset.max_margin_pct_per_pair,
            "recommended_bots": list(preset.recommended_bots),
        },
        "concurrent_positions_at_min_lot": result.concurrent_positions_at_min_lot,
        "suggestions": [
            {
                "symbol": s.symbol,
                "init_margin_usd": s.init_margin_usd,
                "max_lot_fit": s.max_lot_fit,
                "margin_pct_of_balance": s.margin_pct_of_balance,
                "fits": s.fits,
                "warn": s.warn,
                "typical_daily_pct": s.typical_daily_pct,
                "note": s.note,
                "broker_confirms_at_order_time": s.broker_confirms_at_order_time,
            }
            for s in result.suggestions
        ],
        "skipped": result.skipped,
    }
