"""Load approved partner strategies into the runtime registry.

Two delivery types:
  - "source": the partner's uploaded module. We re-validate (defense in
    depth — the same AST scan the upload route ran) then ``exec`` it in a
    synthetic module namespace and register the resulting ``Strategy``
    subclass. No file is written to disk: the ``PartnerSubmission`` row is
    the single source of truth, re-loaded on each process start.
  - "http": an external endpoint. We register an ``HttpProxyStrategy`` bound
    to the endpoint + decrypted HMAC secret. Nothing of the partner's code
    runs in our process.

``exec`` of partner source is gated by (a) the AST safety scan, (b) explicit
human approval, and (c) per-strategy account isolation — see
``app/core/strategy_validator.py``.
"""
from __future__ import annotations

import logging
import types

from app.core.crypto import decrypt
from app.core.strategy_validator import validate_strategy_source
from app.strategies.base import Strategy
from app.strategies.registry import register_http_proxy, register_partner_class

logger = logging.getLogger(__name__)


def _find_strategy_class(ns: dict) -> type[Strategy] | None:
    for value in ns.values():
        if (
            isinstance(value, type)
            and issubclass(value, Strategy)
            and value is not Strategy
        ):
            return value
    return None


def load_source_strategy(slug: str, source: str) -> type[Strategy]:
    """Validate + exec partner source and register the Strategy under ``slug``."""
    res = validate_strategy_source(source)
    if not res.ok:
        codes = ", ".join(f.code for f in res.findings if f.level == "block")
        raise ValueError(f"strategy source failed validation: {codes}")

    module = types.ModuleType(f"app.strategies._partner_{slug}")
    code = compile(source, f"<partner:{slug}>", "exec")
    exec(code, module.__dict__)  # noqa: S102 — gated by AST scan + human approval

    cls = _find_strategy_class(module.__dict__)
    if cls is None:
        raise ValueError(f"no Strategy subclass found in partner source for {slug!r}")
    register_partner_class(slug, cls, overwrite=True)
    return cls


def load_http_strategy(
    slug: str, endpoint_url: str, encrypted_secret: str | None
) -> None:
    """Register an external-HTTP partner strategy under ``slug``."""
    secret = decrypt(encrypted_secret) if encrypted_secret else ""
    register_http_proxy(slug, endpoint_url, secret or "", overwrite=True)


def load_submission(sub) -> None:
    """Register one approved ``PartnerSubmission`` into the runtime registry."""
    if sub.delivery_type == "http":
        if not sub.endpoint_url:
            raise ValueError(f"submission {sub.id} is http but has no endpoint_url")
        load_http_strategy(sub.strategy_slug, sub.endpoint_url, sub.endpoint_secret)
    else:
        if not sub.source_code:
            raise ValueError(f"submission {sub.id} is source but has no source_code")
        load_source_strategy(sub.strategy_slug, sub.source_code)


def load_all_approved(db) -> int:
    """Startup hook: register every approved partner submission.

    Best-effort — a single bad strategy is logged and skipped so it can
    never block app boot. Returns the number successfully registered.
    """
    from app.db.models import PartnerSubmission

    rows = (
        db.query(PartnerSubmission)
        .filter(PartnerSubmission.status == "approved")
        .all()
    )
    loaded = 0
    for sub in rows:
        try:
            load_submission(sub)
            loaded += 1
        except Exception as exc:  # noqa: BLE001 — never block boot
            logger.warning(
                "partner strategy %r failed to load: %s", sub.strategy_slug, exc
            )
    if loaded:
        logger.info("loaded %d approved partner strategies", loaded)
    return loaded
