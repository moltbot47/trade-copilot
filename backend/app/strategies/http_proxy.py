"""HttpProxyStrategy — run a partner strategy that lives on the partner's
own server, so none of their code ever executes in our process.

On each bar the proxy POSTs the recent bar window to the partner's
``endpoint_url`` (HMAC-signed with their secret) and parses a signal from
the JSON response. This is the safest delivery format: zero remote-code
execution on our host, and the partner's source stays private.

Wire contract (what the partner implements — surfaced on the upload form)
------------------------------------------------------------------------
Request  ``POST <endpoint_url>``::

    Headers:
      Content-Type: application/json
      X-Strategy:   <slug>
      X-Timestamp:  <unix-ms>
      X-Signature:  sha256=<hmac-sha256(secret, raw-body)>
    Body:
      {
        "symbol": "NAS100",
        "timeframe": "1m",
        "ts": 1718040000000,
        "bars": [
          {"ts": "2026-06-10T13:59:00+00:00",
           "open": 1.0, "high": 1.1, "low": 0.9, "close": 1.05, "volume": 12},
          ...
        ]
      }

Response  ``200 application/json``. Either no-trade::

      {"signal": null}          # or {}  or  {"signal": false}

or a trade (fields may be nested under "signal" or at top level)::

      {"signal": {
         "side": "buy",                       # required: buy|sell
         "entry_price": 1.05,                 # required
         "stop_loss": 1.00,                   # required
         "take_profit": 1.15,                 # required
         "qty": 0.01,                         # optional (default 0.01)
         "expected_entry_price": 1.05,        # audit field
         "hard_stop_distance_pts": 0.05,      # audit field
         "early_stop_condition": "...",       # audit field
         "trailing_stop_distance_pts": 0.0    # audit field
      }}

Safety: any error — timeout, non-2xx, malformed body, missing/invalid
required field — yields ``None`` (no trade) and is logged. The proxy never
raises into the runner's tick loop.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
from typing import Optional

import httpx
import pandas as pd

from app.strategies.base import Strategy, StrategySignal

logger = logging.getLogger(__name__)

# Cap the bar window we serialize per request — the runner hands us ~240
# bars; sending more just inflates latency.
_MAX_BARS = 240

# Per-bar HTTP budget. The partner runs remote logic, so allow a little
# headroom, but never block the tick loop indefinitely.
_DEFAULT_TIMEOUT_SEC = 6.0


def _sign(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


class HttpProxyStrategy(Strategy):
    """A Strategy whose decisions are made by a remote partner endpoint."""

    def __init__(
        self,
        *,
        slug: str,
        endpoint_url: str,
        secret: str,
        timeframe: str = "1m",
        timeout: float = _DEFAULT_TIMEOUT_SEC,
        max_bars: int = _MAX_BARS,
        client: Optional[httpx.AsyncClient] = None,
    ) -> None:
        self.name = slug
        self.slug = slug
        self.timeframe = timeframe
        self.endpoint_url = endpoint_url
        self._secret = secret or ""
        self._timeout = timeout
        self._max_bars = max_bars
        # Optional injected client (tests). When None we open one per call so
        # there's no long-lived connection to clean up from the tick loop.
        self._client = client

    # ------------------------------------------------------------------ #
    def _serialize_bars(self, symbol: str, bars: pd.DataFrame) -> bytes:
        window = bars.tail(self._max_bars)
        records = []
        for ts, row in window.iterrows():
            records.append(
                {
                    "ts": ts.isoformat() if hasattr(ts, "isoformat") else str(ts),
                    "open": float(row["open"]),
                    "high": float(row["high"]),
                    "low": float(row["low"]),
                    "close": float(row["close"]),
                    "volume": float(row.get("volume", 0.0)),
                }
            )
        payload = {
            "symbol": symbol,
            "timeframe": self.timeframe,
            "ts": int(time.time() * 1000),
            "bars": records,
        }
        # Compact, stable separators so the signature matches what the
        # partner recomputes over the raw bytes.
        return json.dumps(payload, separators=(",", ":")).encode()

    def _parse_signal(self, symbol: str, data: object) -> Optional[StrategySignal]:
        if not isinstance(data, dict):
            return None
        sig = data.get("signal", data)
        # Explicit no-trade encodings.
        if sig is None or sig is False or sig == {}:
            return None
        if not isinstance(sig, dict):
            return None

        side = str(sig.get("side", "")).lower()
        if side not in ("buy", "sell"):
            logger.warning("http_proxy %s: bad/absent side %r", self.slug, sig.get("side"))
            return None
        try:
            entry = float(sig["entry_price"])
            stop = float(sig["stop_loss"])
            take = float(sig["take_profit"])
        except (KeyError, TypeError, ValueError):
            logger.warning("http_proxy %s: missing/invalid price field in %s", self.slug, sig)
            return None

        try:
            qty = float(sig.get("qty", 0.01))
        except (TypeError, ValueError):
            qty = 0.01
        if qty <= 0:
            qty = 0.01

        def _f(key: str, default: float = 0.0) -> float:
            try:
                return float(sig.get(key, default))
            except (TypeError, ValueError):
                return default

        return StrategySignal(
            # Always trust OUR symbol, never the partner's echo.
            symbol=symbol,
            side=side,
            entry_price=entry,
            stop_loss=stop,
            take_profit=take,
            qty=qty,
            expected_entry_price=_f("expected_entry_price", entry),
            hard_stop_distance_pts=_f("hard_stop_distance_pts", abs(entry - stop)),
            early_stop_condition=str(sig.get("early_stop_condition", "")),
            trailing_stop_distance_pts=_f("trailing_stop_distance_pts"),
            extra={"source": "http_proxy", "slug": self.slug},
        )

    async def on_bar(
        self, symbol: str, bars: pd.DataFrame
    ) -> Optional[StrategySignal]:
        if bars is None or len(bars) == 0:
            return None
        try:
            body = self._serialize_bars(symbol, bars)
        except Exception as exc:  # serialization must never crash the loop
            logger.warning("http_proxy %s: bar serialization failed: %s", self.slug, exc)
            return None

        headers = {
            "Content-Type": "application/json",
            "X-Strategy": self.slug,
            "X-Timestamp": str(int(time.time() * 1000)),
            "X-Signature": _sign(self._secret, body),
        }

        try:
            if self._client is not None:
                resp = await self._client.post(
                    self.endpoint_url, content=body, headers=headers, timeout=self._timeout
                )
            else:
                async with httpx.AsyncClient(timeout=self._timeout) as client:
                    resp = await client.post(
                        self.endpoint_url, content=body, headers=headers
                    )
        except (httpx.HTTPError, httpx.TimeoutException) as exc:
            logger.warning("http_proxy %s: request failed: %s", self.slug, exc)
            return None

        if resp.status_code // 100 != 2:
            logger.warning(
                "http_proxy %s: endpoint returned %s", self.slug, resp.status_code
            )
            return None

        try:
            data = resp.json()
        except (json.JSONDecodeError, ValueError) as exc:
            logger.warning("http_proxy %s: non-JSON response: %s", self.slug, exc)
            return None

        return self._parse_signal(symbol, data)
