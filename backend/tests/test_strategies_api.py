"""Strategy API tests (status/start/stop). Mocks the runner internals."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app.core.crypto import encrypt
from app.db.models import User


def _ensure_connected_user(db_session, email: str = "t@example.com") -> User:
    """Seed a User row with a valid-looking TL connection.

    The /strategy/start endpoint pre-validates each user's TL session.
    Tests that use a placeholder user_email must seed the row first AND
    mock validate_user_tl_session — otherwise the endpoint correctly
    refuses to start.
    """
    user = db_session.query(User).filter(User.email == email).first()
    if user is None:
        user = User(email=email, hashed_password="x")
        user.tradelocker_account_id = "ACC-1"
        user.tradelocker_acc_num = "1"
        user.tradelocker_env = "demo"
        user.tradelocker_token = encrypt("fake-access-token")
        db_session.add(user)
        db_session.commit()
    return user


async def _always_valid_session(_user_id):
    """Mock for validate_user_tl_session — pretend the session is healthy."""
    return True, "ok"


def test_status_unknown_bot_returns_empty_state(client, seed_bots):
    """The recent fix — /status should return 200 + structured empty state, not 404."""
    res = client.get("/api/strategy/status?bot_id=9999&timeframe=1m")
    assert res.status_code == 200
    body = res.json()
    assert "state" in body
    assert body["state"]["is_running"] is False
    assert body["state"]["bot_id"] == 9999
    assert body["state"]["timeframe"] == "1m"
    assert body["recent_trades"] == []
    assert body["recent_outcomes"] == []
    assert body["performance"] is None


def test_status_for_existing_bot_no_state_returns_empty(client, seed_bots):
    bot_id = seed_bots[0].id  # not yet started → no StrategyState row
    res = client.get(f"/api/strategy/status?bot_id={bot_id}&timeframe=1m")
    assert res.status_code == 200
    assert res.json()["state"]["is_running"] is False


def test_start_requires_latpfn_bot(client, seed_bots):
    # ORB bot is NOT a latpfn_* strategy → webhook-driven, /start should reject
    orb = next(b for b in seed_bots if b.slug == "orb-breakout")
    res = client.post(
        "/api/strategy/start",
        json={
            "bot_id": orb.id,
            "timeframe": "1m",
            "symbols": ["BTCUSD"],
            "user_emails": ["t@example.com"],
        },
    )
    assert res.status_code == 400
    assert "webhook" in res.json()["detail"].lower()


def test_start_unknown_bot_returns_404(client, seed_bots):
    res = client.post(
        "/api/strategy/start",
        json={
            "bot_id": 9999,
            "timeframe": "1m",
            "symbols": ["BTCUSD"],
            "user_emails": ["t@example.com"],
        },
    )
    assert res.status_code == 404


def test_start_validates_symbols_required(client, seed_bots):
    latpfn = next(b for b in seed_bots if b.slug == "latpfn-momentum")
    res = client.post(
        "/api/strategy/start",
        json={
            "bot_id": latpfn.id,
            "timeframe": "1m",
            "symbols": [],
            "user_emails": ["t@example.com"],
        },
    )
    assert res.status_code == 400


def test_start_validates_user_emails_required(client, seed_bots):
    latpfn = next(b for b in seed_bots if b.slug == "latpfn-momentum")
    res = client.post(
        "/api/strategy/start",
        json={
            "bot_id": latpfn.id,
            "timeframe": "1m",
            "symbols": ["BTCUSD"],
            "user_emails": [],
        },
    )
    assert res.status_code == 400


def test_start_launches_runner_with_mock(client, seed_bots, db_session):
    _ensure_connected_user(db_session)
    latpfn = next(b for b in seed_bots if b.slug == "latpfn-momentum")
    fake_task = SimpleNamespace(done=lambda: False)
    fake_runner = SimpleNamespace(
        bot_id=latpfn.id,
        timeframe="1m",
        symbols=["BTCUSD"],
        user_emails=["t@example.com"],
        task=fake_task,
    )
    with patch(
        "app.strategies.api.StrategyRunner.start",
        new=AsyncMock(return_value=fake_runner),
    ), patch(
        "app.strategies.api.validate_user_tl_session", new=_always_valid_session
    ):
        res = client.post(
            "/api/strategy/start",
            json={
                "bot_id": latpfn.id,
                "timeframe": "1m",
                "symbols": ["BTCUSD"],
                "user_emails": ["t@example.com"],
            },
        )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["status"] == "started"
    assert body["bot_id"] == latpfn.id
    assert body["runner_type"] == "SimpleNamespace"  # mocked instance class


def test_start_dispatches_quant_bot_to_quant_runner(client, seed_bots, db_session):
    """latpfn_quant bot id must be routed to QuantRunner.start, NOT StrategyRunner."""
    _ensure_connected_user(db_session)
    quant = next(b for b in seed_bots if b.slug == "latpfn-quant")
    fake_task = SimpleNamespace(done=lambda: False)
    fake_runner = SimpleNamespace(
        bot_id=quant.id,
        timeframe="1m",
        symbols=["BTCUSD"],
        user_emails=["t@example.com"],
        task=fake_task,
    )
    momentum_mock = AsyncMock(return_value=fake_runner)
    quant_mock = AsyncMock(return_value=fake_runner)
    with patch("app.strategies.api.StrategyRunner.start", new=momentum_mock), patch(
        "app.strategies.api.QuantRunner.start", new=quant_mock
    ), patch(
        "app.strategies.api.validate_user_tl_session", new=_always_valid_session
    ):
        res = client.post(
            "/api/strategy/start",
            json={
                "bot_id": quant.id,
                "timeframe": "1m",
                "symbols": ["BTCUSD"],
                "user_emails": ["t@example.com"],
            },
        )
    assert res.status_code == 200, res.text
    quant_mock.assert_awaited_once()
    momentum_mock.assert_not_called()
    body = res.json()
    assert body["status"] == "started"
    assert body["bot_id"] == quant.id


def test_start_dispatches_momentum_bot_to_strategy_runner(client, seed_bots, db_session):
    """latpfn_momentum must hit StrategyRunner, not QuantRunner."""
    _ensure_connected_user(db_session)
    momentum = next(b for b in seed_bots if b.slug == "latpfn-momentum")
    fake_task = SimpleNamespace(done=lambda: False)
    fake_runner = SimpleNamespace(
        bot_id=momentum.id,
        timeframe="1m",
        symbols=["BTCUSD"],
        user_emails=["t@example.com"],
        task=fake_task,
    )
    momentum_mock = AsyncMock(return_value=fake_runner)
    quant_mock = AsyncMock(return_value=fake_runner)
    with patch("app.strategies.api.StrategyRunner.start", new=momentum_mock), patch(
        "app.strategies.api.QuantRunner.start", new=quant_mock
    ), patch(
        "app.strategies.api.validate_user_tl_session", new=_always_valid_session
    ):
        res = client.post(
            "/api/strategy/start",
            json={
                "bot_id": momentum.id,
                "timeframe": "1m",
                "symbols": ["BTCUSD"],
                "user_emails": ["t@example.com"],
            },
        )
    assert res.status_code == 200, res.text
    momentum_mock.assert_awaited_once()
    quant_mock.assert_not_called()


def test_stop_when_not_running(client, seed_bots):
    latpfn = next(b for b in seed_bots if b.slug == "latpfn-momentum")
    res = client.post(
        "/api/strategy/stop",
        json={"bot_id": latpfn.id, "timeframe": "1m"},
    )
    assert res.status_code == 200
    assert res.json()["status"] == "not_running"


def test_stop_when_running_cancels(client, seed_bots):
    latpfn = next(b for b in seed_bots if b.slug == "latpfn-momentum")

    fake_runner = SimpleNamespace(stop=AsyncMock())
    with patch("app.strategies.api.get_runner", return_value=fake_runner):
        res = client.post(
            "/api/strategy/stop",
            json={"bot_id": latpfn.id, "timeframe": "1m"},
        )
    assert res.status_code == 200
    assert res.json()["status"] == "stopped"
    fake_runner.stop.assert_awaited_once()


def test_equity_returns_empty_for_no_trades(client, seed_bots):
    bot_id = seed_bots[0].id
    res = client.get(f"/api/strategy/equity?bot_id={bot_id}")
    assert res.status_code == 200
    body = res.json()
    assert body["bot_id"] == bot_id
    assert body["timestamps"] == []
    assert body["cumulative_r"] == []
    assert body["cumulative_pnl_usd"] == []
    assert body["total_trades"] == 0
