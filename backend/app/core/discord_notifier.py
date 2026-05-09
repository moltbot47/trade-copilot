"""Post signal embeds to a Discord webhook. No-op if not configured."""
from __future__ import annotations

import logging
from typing import Optional

import httpx

from app.config import get_settings

logger = logging.getLogger(__name__)


async def post_signal_to_discord(
    bot_name: str,
    instrument: str,
    side: str,
    entry: Optional[float],
    sl: Optional[float],
    tp: Optional[float],
    base_lot: float,
) -> bool:
    settings = get_settings()
    url = settings.DISCORD_WEBHOOK_URL
    if not url:
        return False

    color = 0x2ECC71 if side.lower() == "buy" else 0xE74C3C
    description_lines = [
        f"**{instrument} {side.upper()}** @ `{entry if entry is not None else 'market'}`",
        f"SL: `{sl}` | TP: `{tp}`",
        f"Base lot: `{base_lot}`",
    ]
    payload = {
        "username": "Trade Copilot",
        "embeds": [
            {
                "title": f":rotating_light: NEW SIGNAL - {bot_name}",
                "description": "\n".join(description_lines),
                "color": color,
            }
        ],
    }

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(url, json=payload)
        return 200 <= resp.status_code < 300
    except httpx.HTTPError as exc:
        logger.warning("discord post failed: %s", exc)
        return False
