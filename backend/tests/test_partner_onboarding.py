"""Tests for partner self-serve onboarding — invites, upload, approval.

The headline risks: (a) a malicious source upload must be blocked BEFORE it
is stored or run, (b) owner-only endpoints must require auth, (c) approval
must wire up exactly one scoped VIEWER grant + a registry-dispatched bot,
and (d) the single-use invite must not be burned by a rejected upload.
"""
from __future__ import annotations

import pytest

from app.core.crypto import decrypt
from app.db.models import (
    AccountAccessGrant,
    AccountAccessRole,
    Bot,
    PartnerInvite,
    PartnerSubmission,
    StrategyAccount,
    StrategyState,
    StrategyType,
    TradingAccount,
    User,
)

CLEAN_SOURCE = b'''
from __future__ import annotations
import pandas as pd
from app.strategies.base import Strategy, StrategySignal


class VelocitySpike(Strategy):
    name = "velocity_spike"
    timeframe = "1m"

    def __init__(self, *, params=None):
        self.params = params or {}

    async def on_bar(self, symbol, bars):
        return None
'''

MALICIOUS_SOURCE = b'''
import os
from app.strategies.base import Strategy


class Evil(Strategy):
    name = "evil"
    async def on_bar(self, symbol, bars):
        os.system("echo pwned")
        return None
'''


@pytest.fixture(autouse=True)
def _clean_registry():
    """Snapshot/restore the global strategy registry around each test."""
    from app.strategies import registry

    before = dict(registry._REGISTRY)
    yield
    registry._REGISTRY.clear()
    registry._REGISTRY.update(before)


@pytest.fixture
def owner_account(db_session, auth_headers):
    """A TradingAccount owned by the auth'd user (tester@example.com)."""
    owner = db_session.query(User).filter_by(email="tester@example.com").first()
    ta = TradingAccount(
        owner_user_id=owner.id,
        label="Audit demo",
        tradelocker_account_id="2163244",
        tradelocker_acc_num="4",
        tradelocker_env="demo",
    )
    db_session.add(ta)
    db_session.commit()
    return ta


def _create_invite(client, auth_headers, **kw):
    res = client.post("/api/partner-invites", json={"label": "Vladimir audit", **kw}, headers=auth_headers)
    assert res.status_code == 200, res.text
    return res.json()


def _submit_source(client, token, source=CLEAN_SOURCE, **overrides):
    data = {
        "partner_name": "Vladimir",
        "partner_email": "vladimir@example.com",
        "strategy_name": "Velocity Spike",
        "delivery_type": "source",
        "instruments_csv": "NAS100",
        "timeframe": "1m",
        **overrides,
    }
    return client.post(
        f"/api/invite/{token}/submit",
        data=data,
        files={"file": ("velocity_spike.py", source, "text/x-python")},
    )


# --------------------------------------------------------------------- #
# invite lifecycle
# --------------------------------------------------------------------- #
def test_create_and_fetch_invite(client, auth_headers):
    inv = _create_invite(client, auth_headers)
    assert inv["state"] == "active"
    assert inv["url_path"] == f"/invite/{inv['token']}"

    # Public landing — no auth, no owner data.
    res = client.get(f"/api/invite/{inv['token']}")
    assert res.status_code == 200
    assert res.json()["label"] == "Vladimir audit"
    assert "created_by_user_id" not in res.json()


def test_owner_endpoints_require_auth(client):
    assert client.post("/api/partner-invites", json={}).status_code in (401, 403)
    assert client.get("/api/partner-invites").status_code in (401, 403)
    assert client.get("/api/partner-submissions").status_code in (401, 403)


def test_get_unknown_invite_404(client):
    assert client.get("/api/invite/nope").status_code == 404


def test_revoked_invite_is_gone(client, auth_headers):
    inv = _create_invite(client, auth_headers)
    assert client.delete(f"/api/partner-invites/{inv['id']}", headers=auth_headers).status_code == 200
    assert client.get(f"/api/invite/{inv['token']}").status_code == 410


# --------------------------------------------------------------------- #
# submission — source path
# --------------------------------------------------------------------- #
def test_submit_clean_source_creates_pending(client, auth_headers, db_session):
    inv = _create_invite(client, auth_headers)
    res = _submit_source(client, inv["token"])
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["status"] == "received"

    sub = db_session.get(PartnerSubmission, body["submission_id"])
    assert sub.status == "pending"
    assert sub.delivery_type == "source"
    assert sub.strategy_slug == "velocity_spike"  # from class name attr
    assert sub.source_code is not None

    # Invite now consumed → second submit is rejected.
    assert _submit_source(client, inv["token"]).status_code == 410


def test_malicious_source_blocked_and_invite_preserved(client, auth_headers, db_session):
    inv = _create_invite(client, auth_headers)
    res = _submit_source(client, inv["token"], source=MALICIOUS_SOURCE)
    assert res.status_code == 422
    detail = res.json()["detail"]
    assert detail["error"] == "strategy_failed_validation"
    codes = {f["code"] for f in detail["findings"]}
    assert "import_not_allowed" in codes

    # Nothing stored, invite NOT burned (partner can fix + resubmit).
    assert db_session.query(PartnerSubmission).count() == 0
    inv_row = db_session.get(PartnerInvite, inv["id"])
    assert inv_row.used_at is None

    # The same link still accepts a clean upload.
    assert _submit_source(client, inv["token"]).status_code == 200


def test_source_requires_file(client, auth_headers):
    inv = _create_invite(client, auth_headers)
    res = client.post(
        f"/api/invite/{inv['token']}/submit",
        data={
            "partner_name": "V",
            "partner_email": "v@example.com",
            "strategy_name": "X",
            "delivery_type": "source",
        },
    )
    assert res.status_code == 400


# --------------------------------------------------------------------- #
# submission — http path
# --------------------------------------------------------------------- #
def test_submit_http_encrypts_secret(client, auth_headers, db_session):
    inv = _create_invite(client, auth_headers)
    res = client.post(
        f"/api/invite/{inv['token']}/submit",
        data={
            "partner_name": "Vladimir",
            "partner_email": "vladimir@example.com",
            "strategy_name": "Remote Spike",
            "delivery_type": "http",
            "endpoint_url": "https://vlad.example.com/signal",
            "endpoint_secret": "topsecret",
            "instruments_csv": "NAS100",
        },
    )
    assert res.status_code == 200, res.text
    sub = db_session.get(PartnerSubmission, res.json()["submission_id"])
    assert sub.delivery_type == "http"
    assert sub.endpoint_url == "https://vlad.example.com/signal"
    # stored encrypted, not plaintext
    assert sub.endpoint_secret != "topsecret"
    assert decrypt(sub.endpoint_secret) == "topsecret"


def test_http_requires_https(client, auth_headers):
    inv = _create_invite(client, auth_headers)
    res = client.post(
        f"/api/invite/{inv['token']}/submit",
        data={
            "partner_name": "V",
            "partner_email": "v@example.com",
            "strategy_name": "X",
            "delivery_type": "http",
            "endpoint_url": "http://insecure.example.com",
            "endpoint_secret": "s",
        },
    )
    assert res.status_code == 400


# --------------------------------------------------------------------- #
# review + approval
# --------------------------------------------------------------------- #
def test_owner_sees_submission_detail(client, auth_headers, db_session):
    inv = _create_invite(client, auth_headers)
    sid = _submit_source(client, inv["token"]).json()["submission_id"]

    lst = client.get("/api/partner-submissions?status=pending", headers=auth_headers)
    assert any(s["id"] == sid for s in lst.json()["items"])

    detail = client.get(f"/api/partner-submissions/{sid}", headers=auth_headers).json()
    assert detail["source_code"] is not None
    assert detail["ast_scan"]["ok"] is True


def test_approve_source_wires_grant_bot_and_registry(
    client, auth_headers, db_session, owner_account
):
    from app.strategies.registry import is_registered

    inv = _create_invite(client, auth_headers)
    sid = _submit_source(client, inv["token"]).json()["submission_id"]

    res = client.post(
        f"/api/partner-submissions/{sid}/approve",
        json={"account_id": owner_account.id, "allowed_instruments_csv": "NAS100"},
        headers=auth_headers,
    )
    assert res.status_code == 200, res.text
    body = res.json()

    # Strategy is now live in the registry → isolation can run it.
    assert is_registered("velocity_spike")

    # Partner user created.
    partner = db_session.query(User).filter_by(email="vladimir@example.com").first()
    assert partner is not None

    # Scoped VIEWER grant on the owner's account.
    grant = db_session.get(AccountAccessGrant, body["grant_id"])
    assert grant.role == AccountAccessRole.viewer
    assert grant.account_id == owner_account.id
    assert grant.grantee_user_id == partner.id
    assert grant.allowed_instruments_csv == "NAS100"

    # Partner bot dispatched by slug.
    bot = db_session.get(Bot, body["bot_id"])
    assert bot.strategy_type == StrategyType.partner
    assert bot.strategy_slug == "velocity_spike"

    # StrategyState seeded for the runner.
    state = db_session.query(StrategyState).filter_by(bot_id=bot.id).first()
    assert state is not None

    # Submission marked approved.
    sub = db_session.get(PartnerSubmission, sid)
    assert sub.status == "approved"
    assert sub.approved_bot_id == bot.id


def test_approve_http_registers_proxy(client, auth_headers, db_session, owner_account):
    from app.strategies.registry import build_strategy, is_registered, StrategyContext

    inv = _create_invite(client, auth_headers)
    sid = client.post(
        f"/api/invite/{inv['token']}/submit",
        data={
            "partner_name": "Vladimir",
            "partner_email": "vladimir@example.com",
            "strategy_name": "Remote Spike",
            "delivery_type": "http",
            "endpoint_url": "https://vlad.example.com/signal",
            "endpoint_secret": "topsecret",
        },
    ).json()["submission_id"]

    res = client.post(
        f"/api/partner-submissions/{sid}/approve",
        json={"account_id": owner_account.id},
        headers=auth_headers,
    )
    assert res.status_code == 200, res.text
    slug = db_session.get(PartnerSubmission, sid).strategy_slug
    assert is_registered(slug)
    # The registered factory builds an HttpProxyStrategy bound to the endpoint.
    strat = build_strategy(slug, StrategyContext(bot_id=1, timeframe="1m"))
    assert strat.endpoint_url == "https://vlad.example.com/signal"


def test_approve_onto_foreign_account_forbidden(
    client, auth_headers, db_session
):
    # Account owned by someone else → owner check rejects.
    stranger = User(email="stranger@x.com", hashed_password="x")
    db_session.add(stranger)
    db_session.commit()
    foreign = TradingAccount(
        owner_user_id=stranger.id,
        tradelocker_account_id="9999",
        tradelocker_acc_num="1",
    )
    db_session.add(foreign)
    db_session.commit()

    inv = _create_invite(client, auth_headers)
    sid = _submit_source(client, inv["token"]).json()["submission_id"]
    res = client.post(
        f"/api/partner-submissions/{sid}/approve",
        json={"account_id": foreign.id},
        headers=auth_headers,
    )
    assert res.status_code == 403


def test_double_approve_conflicts(client, auth_headers, owner_account):
    inv = _create_invite(client, auth_headers)
    sid = _submit_source(client, inv["token"]).json()["submission_id"]
    ok = client.post(
        f"/api/partner-submissions/{sid}/approve",
        json={"account_id": owner_account.id},
        headers=auth_headers,
    )
    assert ok.status_code == 200
    again = client.post(
        f"/api/partner-submissions/{sid}/approve",
        json={"account_id": owner_account.id},
        headers=auth_headers,
    )
    assert again.status_code == 409


@pytest.fixture
def live_account(db_session, auth_headers):
    owner = db_session.query(User).filter_by(email="tester@example.com").first()
    ta = TradingAccount(
        owner_user_id=owner.id,
        label="Live acct",
        tradelocker_account_id="7777777",
        tradelocker_acc_num="2",
        tradelocker_env="live",
    )
    db_session.add(ta)
    db_session.commit()
    return ta


def test_auto_start_rejected_on_live_account(client, auth_headers, live_account):
    res = client.post(
        "/api/partner-invites",
        json={"label": "bad", "trading_account_id": live_account.id, "auto_start": True},
        headers=auth_headers,
    )
    assert res.status_code == 400  # live can't auto-start


def test_demo_invite_runs_instantly_without_approval(
    client, auth_headers, db_session, owner_account, monkeypatch
):
    # Stub the isolation runner start so we don't touch a real broker/loop.
    async def _noop_start(*args, **kwargs):
        return object()

    monkeypatch.setattr("app.strategies.isolation.IsolatedRunner.start", _noop_start)

    # Demo-bound, auto_start invite.
    inv = client.post(
        "/api/partner-invites",
        json={
            "label": "Vlad instant demo",
            "trading_account_id": owner_account.id,
            "auto_start": True,
        },
        headers=auth_headers,
    ).json()
    assert inv["auto_start"] is True
    assert inv["account_env"] == "demo"

    # Public landing advertises instant start.
    landing = client.get(f"/api/invite/{inv['token']}").json()
    assert landing["instant_start"] is True
    assert landing["account_label"] == "Audit demo"

    # Partner submits → runs immediately, NO approval step.
    res = _submit_source(client, inv["token"])
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["status"] == "running"
    assert body["started"] is True

    # Submission is already approved (no pending review).
    sub = db_session.get(PartnerSubmission, body["submission_id"])
    assert sub.status == "approved"

    # Viewer grant + partner bot + demo binding all exist.
    bot = db_session.get(Bot, body["bot_id"])
    assert bot.strategy_type == StrategyType.partner
    grant = db_session.query(AccountAccessGrant).filter_by(account_id=owner_account.id).first()
    assert grant is not None and grant.role == AccountAccessRole.viewer
    binding = db_session.query(StrategyAccount).filter_by(bot_id=bot.id).first()
    assert binding is not None
    assert binding.tradelocker_account_id == owner_account.tradelocker_account_id
    assert binding.tradelocker_env == "demo"


def test_demo_autostart_bad_upload_does_not_burn_invite(
    client, auth_headers, db_session, owner_account
):
    inv = client.post(
        "/api/partner-invites",
        json={"label": "x", "trading_account_id": owner_account.id, "auto_start": True},
        headers=auth_headers,
    ).json()
    res = _submit_source(client, inv["token"], source=MALICIOUS_SOURCE)
    assert res.status_code == 422
    # Nothing provisioned, invite still usable.
    assert db_session.query(PartnerSubmission).count() == 0
    assert db_session.get(PartnerInvite, inv["id"]).used_at is None


def test_update_instruments_on_approved_bot(
    client, auth_headers, db_session, owner_account, monkeypatch
):
    async def _noop_start(*args, **kwargs):
        class _R:
            bot_id = 0
            symbols = ["NAS100", "US30", "XAUUSD"]
            task = None
        return _R()

    monkeypatch.setattr("app.strategies.isolation.IsolatedRunner.start", _noop_start)
    monkeypatch.setattr("app.strategies.isolation.get_iso_runner", lambda b: None)

    inv = client.post(
        "/api/partner-invites",
        json={"label": "x", "trading_account_id": owner_account.id, "auto_start": True},
        headers=auth_headers,
    ).json()
    sid = _submit_source(client, inv["token"]).json()["submission_id"]

    res = client.post(
        f"/api/partner-submissions/{sid}/instruments",
        json={"instruments_csv": "nas100, us30 ,XAUUSD,US30", "restart": True},
        headers=auth_headers,
    )
    assert res.status_code == 200, res.text
    body = res.json()
    # normalized: upper-cased + de-duped, order preserved
    assert body["instruments"] == "NAS100,US30,XAUUSD"
    bot = db_session.get(Bot, body["bot_id"])
    assert bot.instruments_csv == "NAS100,US30,XAUUSD"


def test_reject_marks_rejected(client, auth_headers, db_session):
    inv = _create_invite(client, auth_headers)
    sid = _submit_source(client, inv["token"]).json()["submission_id"]
    res = client.post(
        f"/api/partner-submissions/{sid}/reject",
        json={"reason": "needs a hard stop"},
        headers=auth_headers,
    )
    assert res.status_code == 200
    sub = db_session.get(PartnerSubmission, sid)
    assert sub.status == "rejected"
    assert sub.rejection_reason == "needs a hard stop"
