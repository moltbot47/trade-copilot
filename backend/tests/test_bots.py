"""Bot catalog endpoint tests."""
from __future__ import annotations


def test_list_bots_returns_seeded_bots(client, seed_bots):
    res = client.get("/api/bots")
    assert res.status_code == 200
    bots = res.json()
    assert isinstance(bots, list)
    assert len(bots) >= 3
    slugs = {b["slug"] for b in bots}
    assert {"orb-breakout", "squeeze-momentum", "stoch-hook-reversal"}.issubset(slugs)


def test_list_bots_omits_inactive(client, db_session, seed_bots):
    from app.db.models import Bot

    # Disable one bot
    bot = db_session.query(Bot).filter(Bot.slug == "orb-breakout").first()
    bot.is_active = False
    db_session.commit()

    res = client.get("/api/bots")
    assert res.status_code == 200
    slugs = {b["slug"] for b in res.json()}
    assert "orb-breakout" not in slugs


def test_get_bot_by_id(client, seed_bots):
    target = seed_bots[0]
    res = client.get(f"/api/bots/{target.id}")
    assert res.status_code == 200
    body = res.json()
    assert body["id"] == target.id
    assert body["slug"] == target.slug


def test_get_bot_unknown_returns_404(client, seed_bots):
    res = client.get("/api/bots/9999")
    assert res.status_code == 404
