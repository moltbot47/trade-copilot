"""
Phase 1 Discord Relay — receives TradingView webhooks and broadcasts to Discord.
Run with: python discord_relay.py
Configure DISCORD_WEBHOOK_URL in .env.

Standalone — no DB, no auth, no execution. Pure signal broadcast.
For users who want signals only (no auto-trading).
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException, Request, status
from pydantic import BaseModel, Field

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:  # python-dotenv is optional
    pass


# -----------------------------------------------------------------------------
# Config
# -----------------------------------------------------------------------------
DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "").strip()
PORT = int(os.getenv("DISCORD_RELAY_PORT", "8001"))
SHARED_SECRET = os.getenv("RELAY_SHARED_SECRET", "").strip()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger("discord_relay")


# -----------------------------------------------------------------------------
# Models
# -----------------------------------------------------------------------------
class TradingViewSignal(BaseModel):
    bot_secret: str
    instrument: str
    side: str = Field(..., description="buy or sell")
    entry_price: float
    stop_loss: float | None = None
    take_profit: float | None = None
    base_lot_size: float | None = None


# -----------------------------------------------------------------------------
# App
# -----------------------------------------------------------------------------
app = FastAPI(
    title="Trade Copilot — Discord Relay",
    version="1.0.0",
    description="Phase 1: TradingView -> Discord. No execution.",
)


@app.get("/health")
async def health() -> dict[str, Any]:
    return {
        "status": "ok",
        "discord_configured": bool(DISCORD_WEBHOOK_URL),
        "time": datetime.now(timezone.utc).isoformat(),
    }


@app.post("/webhook", status_code=status.HTTP_202_ACCEPTED)
async def webhook(signal: TradingViewSignal, request: Request) -> dict[str, Any]:
    if not DISCORD_WEBHOOK_URL:
        raise HTTPException(
            status_code=503,
            detail="DISCORD_WEBHOOK_URL not configured. Set it in .env.",
        )

    if SHARED_SECRET and signal.bot_secret != SHARED_SECRET:
        # Optional shared-secret gate. Skipped if SHARED_SECRET is empty.
        log.warning("Rejected signal with bot_secret=%s", signal.bot_secret)
        raise HTTPException(status_code=401, detail="Invalid bot_secret")

    embed = build_embed(signal)
    payload = {"embeds": [embed]}

    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(DISCORD_WEBHOOK_URL, json=payload)

    if resp.status_code >= 300:
        log.error("Discord rejected payload: %s %s", resp.status_code, resp.text)
        raise HTTPException(status_code=502, detail="Discord webhook failed")

    log.info(
        "Relayed %s %s @ %s for bot=%s",
        signal.side,
        signal.instrument,
        signal.entry_price,
        signal.bot_secret,
    )
    return {"relayed": True}


# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------
def build_embed(signal: TradingViewSignal) -> dict[str, Any]:
    side = signal.side.lower()
    is_buy = side in ("buy", "long")
    color = 0x2ECC71 if is_buy else 0xE74C3C  # green / red
    arrow = "BUY" if is_buy else "SELL"

    fields = [
        {"name": "Instrument", "value": signal.instrument, "inline": True},
        {"name": "Side", "value": arrow, "inline": True},
        {"name": "Entry", "value": f"{signal.entry_price:g}", "inline": True},
    ]
    if signal.stop_loss is not None:
        fields.append({"name": "Stop", "value": f"{signal.stop_loss:g}", "inline": True})
    if signal.take_profit is not None:
        fields.append({"name": "Target", "value": f"{signal.take_profit:g}", "inline": True})
    if signal.base_lot_size is not None:
        fields.append({"name": "Base Lot", "value": f"{signal.base_lot_size:g}", "inline": True})

    return {
        "title": f"Trade Copilot Signal — {signal.bot_secret}",
        "color": color,
        "fields": fields,
        "footer": {"text": "Educational use only. Not financial advice."},
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


# -----------------------------------------------------------------------------
# Entry
# -----------------------------------------------------------------------------
if __name__ == "__main__":
    import uvicorn

    log.info("Starting Discord relay on port %s", PORT)
    uvicorn.run(app, host="0.0.0.0", port=PORT)
