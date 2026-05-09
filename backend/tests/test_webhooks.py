"""TradingView webhook tests — fan_out is mocked so no broker calls."""
from __future__ import annotations

from unittest.mock import AsyncMock, patch


def test_webhook_valid_payload_records_signal(client, seed_bots):
    with patch("app.api.webhooks.fan_out", new=AsyncMock(return_value=[])), patch(
        "app.api.webhooks.post_signal_to_discord", new=AsyncMock(return_value=None)
    ):
        res = client.post(
            "/api/webhooks/tradingview",
            json={
                "bot_secret": "orb-breakout",
                "instrument": "EURUSD",
                "side": "buy",
                "entry_price": 1.10,
                "stop_loss": 1.09,
                "take_profit": 1.12,
                "base_lot_size": 0.1,
            },
        )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["status"] == "received"
    assert body["signal_id"] > 0
    assert body["subscribers_notified"] == 0


def test_webhook_invalid_bot_secret_returns_404(client, seed_bots):
    with patch("app.api.webhooks.fan_out", new=AsyncMock(return_value=[])), patch(
        "app.api.webhooks.post_signal_to_discord", new=AsyncMock(return_value=None)
    ):
        res = client.post(
            "/api/webhooks/tradingview",
            json={
                "bot_secret": "no-such-bot",
                "instrument": "EURUSD",
                "side": "buy",
                "entry_price": 1.10,
                "stop_loss": 1.09,
                "take_profit": 1.12,
                "base_lot_size": 0.1,
            },
        )
    assert res.status_code == 404


def test_webhook_inactive_bot_returns_404(client, db_session, seed_bots):
    from app.db.models import Bot

    bot = db_session.query(Bot).filter(Bot.slug == "orb-breakout").first()
    bot.is_active = False
    db_session.commit()

    with patch("app.api.webhooks.fan_out", new=AsyncMock(return_value=[])), patch(
        "app.api.webhooks.post_signal_to_discord", new=AsyncMock(return_value=None)
    ):
        res = client.post(
            "/api/webhooks/tradingview",
            json={
                "bot_secret": "orb-breakout",
                "instrument": "EURUSD",
                "side": "buy",
            },
        )
    assert res.status_code == 404


def test_webhook_missing_required_fields_returns_422(client, seed_bots):
    res = client.post(
        "/api/webhooks/tradingview",
        json={"side": "buy"},  # missing bot_secret + instrument
    )
    assert res.status_code == 422


def test_webhook_invalid_side_returns_422(client, seed_bots):
    res = client.post(
        "/api/webhooks/tradingview",
        json={
            "bot_secret": "orb-breakout",
            "instrument": "EURUSD",
            "side": "diagonal",  # not buy|sell
        },
    )
    assert res.status_code == 422


def test_webhook_discord_failure_does_not_break_endpoint(client, seed_bots):
    """Discord notify is best-effort. A failure must not propagate."""
    with patch(
        "app.api.webhooks.post_signal_to_discord",
        new=AsyncMock(side_effect=RuntimeError("discord down")),
    ), patch("app.api.webhooks.fan_out", new=AsyncMock(return_value=[])):
        res = client.post(
            "/api/webhooks/tradingview",
            json={
                "bot_secret": "orb-breakout",
                "instrument": "EURUSD",
                "side": "sell",
            },
        )
    assert res.status_code == 200
