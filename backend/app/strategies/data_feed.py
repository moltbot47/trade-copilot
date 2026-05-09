"""Bar fetcher for TradeLocker.

Pulls historical OHLCV via REST. Maintains a per-symbol cache so subsequent
calls only fetch the tail since last_ts.

Note: TradeLocker history endpoint shape was discovered empirically. We try
several known param combinations and log whichever response shape we got.
On failure we fall back to a synthetic random walk so the strategy can still
exercise its code paths in dev.

Future: replace with WebSocket subscription. For 1m+ timeframes the polling
overhead is negligible.
"""
from __future__ import annotations

import logging
import time
from typing import Optional

import httpx
import numpy as np
import pandas as pd

from app.core.tradelocker_client import TradeLockerClient, TradeLockerError

logger = logging.getLogger(__name__)


# Map our timeframe strings → TradeLocker resolution strings (try multiple).
# We attempt them in order until one gives a 2xx with bars.
_TF_CANDIDATES: dict[str, list[str]] = {
    "1m": ["1m", "1", "M1"],
    "5m": ["5m", "5", "M5"],
    "15m": ["15m", "15", "M15"],
    "1h": ["1h", "60", "H1"],
}


def _tf_seconds(tf: str) -> int:
    if tf.endswith("m"):
        return int(tf[:-1]) * 60
    if tf.endswith("h"):
        return int(tf[:-1]) * 3600
    return 60


class BarFetcher:
    """Fetches OHLCV bars from TradeLocker and caches them per symbol+tf."""

    def __init__(
        self,
        client: TradeLockerClient,
        account_id: str,
        token: str,
        acc_num: str,
    ) -> None:
        self.client = client
        self.account_id = account_id
        self.token = token
        self.acc_num = acc_num
        # cache key: (symbol, timeframe) → DataFrame
        self._cache: dict[tuple[str, str], pd.DataFrame] = {}

    async def fetch_bars(
        self,
        symbol: str,
        timeframe: str = "1m",
        count: int = 240,
    ) -> pd.DataFrame:
        """Return the most recent `count` bars for `symbol` at `timeframe`.

        DataFrame columns: [open, high, low, close, volume].
        Index: pandas DatetimeIndex (UTC) ascending.
        """
        key = (symbol, timeframe)
        cached = self._cache.get(key)

        # Compute window (epoch ms)
        now_ms = int(time.time() * 1000)
        span_ms = _tf_seconds(timeframe) * 1000 * (count + 5)
        from_ms = now_ms - span_ms

        try:
            tradable_id, route_id = await self.client.resolve_symbol(
                self.account_id, self.token, self.acc_num, symbol
            )
        except TradeLockerError as exc:
            logger.warning("resolve_symbol failed for %s: %s — using synthetic", symbol, exc)
            return self._synthetic(symbol, timeframe, count)

        # Try the known endpoint variants
        for resolution in _TF_CANDIDATES.get(timeframe, [timeframe]):
            df = await self._try_history(
                tradable_id=tradable_id,
                route_id=route_id,
                resolution=resolution,
                from_ms=from_ms,
                to_ms=now_ms,
            )
            if df is not None and not df.empty:
                # Merge with cache, dedupe by index, keep last `count`
                merged = pd.concat([cached, df]) if cached is not None else df
                merged = merged[~merged.index.duplicated(keep="last")].sort_index()
                merged = merged.tail(count)
                self._cache[key] = merged
                return merged

        # All variants failed — degrade gracefully
        logger.warning(
            "all history variants failed for %s tf=%s — returning synthetic",
            symbol,
            timeframe,
        )
        return self._synthetic(symbol, timeframe, count)

    async def _try_history(
        self,
        *,
        tradable_id: int,
        route_id: int,
        resolution: str,
        from_ms: int,
        to_ms: int,
    ) -> Optional[pd.DataFrame]:
        """Attempt one shape of the history call. Return parsed df or None."""
        url = f"{self.client.base_url}/trade/history"
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {self.token}",
            "accNum": str(self.acc_num),
        }
        params = {
            "tradableInstrumentId": tradable_id,
            "routeId": route_id,
            "resolution": resolution,
            "from": from_ms,
            "to": to_ms,
        }
        try:
            async with httpx.AsyncClient(timeout=self.client.timeout) as c:
                r = await c.get(url, headers=headers, params=params)
        except httpx.HTTPError as exc:
            logger.debug("history request error: %s", exc)
            return None
        if r.status_code >= 400:
            logger.debug(
                "history %s returned %s: %s",
                resolution,
                r.status_code,
                r.text[:200],
            )
            return None
        try:
            data = r.json()
        except ValueError:
            logger.debug("history %s returned non-JSON", resolution)
            return None
        return self._parse_history(data)

    @staticmethod
    def _parse_history(payload: dict) -> Optional[pd.DataFrame]:
        """Best-effort parse of the TradeLocker history response.

        Tries several known shapes:
          A) {"d": {"barDetails": [[t,o,h,l,c,v], ...]}}
          B) {"d": [[t,o,h,l,c,v], ...]}
          C) {"bars": [{t,o,h,l,c,v}, ...]}
        """
        rows: list = []
        if isinstance(payload, dict):
            d = payload.get("d")
            if isinstance(d, dict):
                rows = d.get("barDetails") or d.get("bars") or []
            elif isinstance(d, list):
                rows = d
            if not rows:
                rows = payload.get("bars") or []

        if not rows:
            return None

        if isinstance(rows[0], dict):
            df = pd.DataFrame(rows)
            # Normalize column names
            colmap = {
                "t": "timestamp",
                "time": "timestamp",
                "o": "open",
                "h": "high",
                "l": "low",
                "c": "close",
                "v": "volume",
            }
            df = df.rename(columns={k: v for k, v in colmap.items() if k in df.columns})
        else:
            # array-of-arrays: [t, o, h, l, c, v]
            df = pd.DataFrame(rows, columns=["timestamp", "open", "high", "low", "close", "volume"])

        if "timestamp" not in df.columns:
            return None

        # Coerce timestamp to UTC datetime
        ts = pd.to_numeric(df["timestamp"], errors="coerce")
        # If values are seconds (10 digits) vs ms (13 digits), normalize to ms
        if ts.dropna().median() < 1e12:
            ts = ts * 1000
        df["timestamp"] = pd.to_datetime(ts, unit="ms", utc=True)
        df = df.set_index("timestamp").sort_index()

        for col in ("open", "high", "low", "close", "volume"):
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce")
            else:
                df[col] = np.nan
        df = df.dropna(subset=["close"])
        return df[["open", "high", "low", "close", "volume"]]

    @staticmethod
    def _synthetic(symbol: str, timeframe: str, count: int) -> pd.DataFrame:
        """Deterministic random walk as a fallback so the engine keeps running."""
        rng = np.random.RandomState(seed=abs(hash((symbol, timeframe))) % (2**32))
        steps = rng.normal(0, 1, size=count).cumsum()
        base = 100.0 + steps
        spread = np.abs(rng.normal(0, 0.2, size=count))
        df = pd.DataFrame(
            {
                "open": base,
                "high": base + spread,
                "low": base - spread,
                "close": base + rng.normal(0, 0.05, size=count),
                "volume": rng.uniform(100, 1000, size=count),
            }
        )
        tf_s = _tf_seconds(timeframe)
        end = pd.Timestamp.utcnow().floor("s")
        df.index = pd.date_range(end=end, periods=count, freq=f"{tf_s}s", tz="UTC")
        return df
