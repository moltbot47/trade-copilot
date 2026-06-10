"""Strategy registry — map a string key to a Strategy factory.

Before this module the runner dispatched on ``bot.strategy_type`` via a
hardcoded ``if/else`` in three files, so only the two built-in LaT-PFN
strategies could ever run. Partner strategies
(``docs/PARTNER_STRATEGY_SPEC.md``) need to be added at runtime without
editing that dispatch. The registry replaces those branches with a dict
lookup.

Keys
----
* Built-in strategies register under their ``StrategyType`` value
  ("latpfn_momentum", "latpfn_quant"). Always available — registered at
  import time below.
* Partner strategies register under their unique slug, via either:
    - a source module decorated with ``@register_strategy`` (uses the
      class-level ``name``), or
    - an external HTTP endpoint, via ``register_http_proxy(slug, url, secret)``.

Every factory takes a :class:`StrategyContext` and returns a ``Strategy``.
Built-in factories use *lazy imports* so this module never imports the heavy
strategy modules at import time — that keeps ``runner.py`` (which imports
this module) free of any import cycle.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional

from app.strategies.base import Strategy


@dataclass
class StrategyContext:
    """Everything a factory might need to construct a Strategy.

    Built-in strategies use ``latpfn_client`` + ``threshold``; partner
    strategies use ``params`` (their tunable config). A factory takes only
    what it needs and ignores the rest.
    """

    bot_id: int
    timeframe: str
    latpfn_client: object | None = None
    threshold: float = 0.8
    params: Optional[dict] = None


StrategyFactory = Callable[[StrategyContext], Strategy]

_REGISTRY: dict[str, StrategyFactory] = {}


class StrategyNotRegistered(KeyError):
    """Raised by :func:`build_strategy` when a key has no factory."""


def _norm(key: str) -> str:
    return (key or "").strip()


def register_factory(
    key: str, factory: StrategyFactory, *, overwrite: bool = False
) -> None:
    """Register a raw factory under ``key``.

    Idempotent for the *same* factory object (module re-import on startup is
    safe). A *different* factory under an existing key raises unless
    ``overwrite=True`` — partner re-approval passes ``overwrite=True``.
    """
    key = _norm(key)
    if not key:
        raise ValueError("strategy key must be non-empty")
    existing = _REGISTRY.get(key)
    if existing is not None and existing is not factory and not overwrite:
        raise ValueError(f"strategy key {key!r} already registered")
    _REGISTRY[key] = factory


def register_partner_class(
    key: str, cls: type[Strategy], *, overwrite: bool = False
) -> None:
    """Register a partner ``Strategy`` subclass.

    Per the spec the partner class takes ``__init__(self, *, params=None)``.
    """

    def _factory(ctx: StrategyContext) -> Strategy:
        return cls(params=ctx.params or {})

    register_factory(key, _factory, overwrite=overwrite)


def register_strategy(cls: type[Strategy]) -> type[Strategy]:
    """Class decorator — register a partner strategy under its ``name``.

    Usage in a partner module::

        @register_strategy
        class VelocitySpike(Strategy):
            name = "velocity_spike"
            ...
    """
    name = getattr(cls, "name", None)
    if not name or name == "base":
        raise ValueError(
            "@register_strategy requires a unique class-level `name`"
        )
    register_partner_class(name, cls, overwrite=True)
    return cls


def register_http_proxy(
    slug: str, endpoint_url: str, secret: str, *, overwrite: bool = True
) -> None:
    """Register an external-HTTP partner strategy bound to its endpoint.

    The partner hosts the strategy; on each bar the proxy POSTs the bar
    window to ``endpoint_url`` (HMAC-signed with ``secret``) and parses the
    returned signal. Nothing of the partner's code runs in our process.
    """

    def _factory(ctx: StrategyContext) -> Strategy:
        # Lazy import: http_proxy pulls httpx; keep registry import-light.
        from app.strategies.http_proxy import HttpProxyStrategy

        return HttpProxyStrategy(
            slug=slug,
            endpoint_url=endpoint_url,
            secret=secret,
            timeframe=ctx.timeframe,
        )

    register_factory(slug, _factory, overwrite=overwrite)


def is_registered(key: str) -> bool:
    return _norm(key) in _REGISTRY


def registered_keys() -> list[str]:
    return sorted(_REGISTRY)


def unregister(key: str) -> None:
    _REGISTRY.pop(_norm(key), None)


def build_strategy(key: str, ctx: StrategyContext) -> Strategy:
    """Look up ``key`` and build a Strategy, or raise StrategyNotRegistered."""
    factory = _REGISTRY.get(_norm(key))
    if factory is None:
        raise StrategyNotRegistered(
            f"no strategy registered under {key!r}; "
            f"known keys: {registered_keys()}"
        )
    return factory(ctx)


# --------------------------------------------------------------------- #
# Built-in strategies — registered at import so they're always available.
# Lazy imports inside the factories keep registry.py free of heavy deps and
# break any import cycle (runner.py imports registry, not the reverse).
# --------------------------------------------------------------------- #
def _momentum_factory(ctx: StrategyContext) -> Strategy:
    from app.strategies.momentum import LatPFNMomentumStrategy

    return LatPFNMomentumStrategy(
        bot_id=ctx.bot_id,
        timeframe=ctx.timeframe,
        latpfn_client=ctx.latpfn_client,
        threshold=ctx.threshold,
    )


def _quant_factory(ctx: StrategyContext) -> Strategy:
    from app.strategies.quant_strategy import LatPFNQuantStrategy

    return LatPFNQuantStrategy(
        bot_id=ctx.bot_id,
        timeframe=ctx.timeframe,
        latpfn_client=ctx.latpfn_client,
        threshold=ctx.threshold,
    )


register_factory("latpfn_momentum", _momentum_factory)
register_factory("latpfn_quant", _quant_factory)
