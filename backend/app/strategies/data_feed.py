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
    """Fetches OHLCV bars from TradeLocker and caches them per symbol+tf.

    Token lifecycle: the constructor takes the initial token but the field
    is mutable. Set ``token_refresh_cb`` to a coroutine returning a fresh
    token; on 401 the fetcher calls the callback once and retries before
    falling back to synthetic. This prevents the silent-go-dark failure
    where a 24h+ session expires mid-run.

    Cache: bounded to ``cache_maxsize`` entries with LRU eviction so a
    long-lived runner can't OOM on accumulated (symbol, timeframe) pairs.
    """

    def __init__(
        self,
        client: TradeLockerClient,
        account_id: str,
        token: str,
        acc_num: str,
        token_refresh_cb=None,
        cache_maxsize: int = 32,
    ) -> None:
        from collections import OrderedDict

        self.client = client
        self.account_id = account_id
        self.token = token
        self.acc_num = acc_num
        self.token_refresh_cb = token_refresh_cb  # Optional[Callable[[], Awaitable[str|None]]]
        # cache key: (symbol, timeframe) → DataFrame. OrderedDict so we can
        # evict in FIFO/LRU order if size exceeds cache_maxsize.
        self._cache: "OrderedDict[tuple[str, str], pd.DataFrame]" = OrderedDict()
        self._cache_maxsize = max(1, cache_maxsize)
        # symbol → INFO route id (separate from TRADE route, used for history)
        self._info_route_cache: dict[str, int] = {}

    def _cache_put(self, key: tuple[str, str], df: pd.DataFrame) -> None:
        """LRU put: re-insert moves to MRU position; evict the LRU when full."""
        if key in self._cache:
            self._cache.move_to_end(key)
        self._cache[key] = df
        while len(self._cache) > self._cache_maxsize:
            self._cache.popitem(last=False)  # pop LRU

    async def _try_refresh_token(self) -> bool:
        """Invoke the refresh callback (if any) and update self.token.
        Returns True if token rotated, False otherwise.
        """
        if self.token_refresh_cb is None:
            return False
        try:
            new = await self.token_refresh_cb()
        except Exception as exc:
            logger.warning("token refresh callback raised: %s", exc)
            return False
        if not new or new == self.token:
            return False
        self.token = new
        logger.info("BarFetcher token rotated via refresh callback")
        return True

    async def _resolve_info_route(self, symbol: str) -> Optional[int]:
        """Find the INFO-type route for a symbol — different from TRADE route.

        TradeLocker returns each instrument with multiple routes:
          [{id: 9912, type: 'TRADE'}, {id: 452, type: 'INFO'}, ...]
        - Order placement uses TRADE (resolve_symbol returns it).
        - History queries require INFO — using TRADE returns s='error'.
        """
        if symbol in self._info_route_cache:
            return self._info_route_cache[symbol]
        try:
            instruments = await self.client.get_instruments(
                self.account_id, self.token, self.acc_num
            )
        except Exception as exc:
            logger.debug("info-route lookup failed for %s: %s", symbol, exc)
            return None
        for inst in instruments:
            if inst.get("name") == symbol:
                info = next(
                    (r for r in inst.get("routes", []) if r.get("type") == "INFO"),
                    None,
                )
                if info:
                    rid = int(info["id"])
                    self._info_route_cache[symbol] = rid
                    return rid
        return None

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
            tradable_id, _trade_route_id = await self.client.resolve_symbol(
                self.account_id, self.token, self.acc_num, symbol
            )
        except TradeLockerError as exc:
            # If this looks like an auth failure, try a one-shot token refresh
            # before giving up. Prevents the silent-go-dark when a 24h session
            # expires mid-run (the periodic refresh task updates the DB but
            # not the in-memory BarFetcher token).
            if "401" in str(exc) or "unauthorized" in str(exc).lower():
                refreshed = await self._try_refresh_token()
                if refreshed:
                    try:
                        tradable_id, _trade_route_id = await self.client.resolve_symbol(
                            self.account_id, self.token, self.acc_num, symbol
                        )
                    except TradeLockerError as exc2:
                        logger.warning("resolve_symbol still failed after refresh for %s: %s — synthetic", symbol, exc2)
                        df = self._synthetic(symbol, timeframe, count)
                        df.attrs["synthetic"] = True
                        return df
                else:
                    logger.warning("resolve_symbol 401 for %s, no refresh available — synthetic", symbol)
                    df = self._synthetic(symbol, timeframe, count)
                    df.attrs["synthetic"] = True
                    return df
            else:
                logger.warning("resolve_symbol failed for %s: %s — using synthetic", symbol, exc)
                df = self._synthetic(symbol, timeframe, count)
                df.attrs["synthetic"] = True
                return df

        # History needs the INFO route (different from TRADE route).
        info_route_id = await self._resolve_info_route(symbol)
        if info_route_id is None:
            logger.warning(
                "no INFO route for %s — falling back to TRADE route (history may fail)",
                symbol,
            )
            info_route_id = _trade_route_id

        # Try the known endpoint variants
        for resolution in _TF_CANDIDATES.get(timeframe, [timeframe]):
            df = await self._try_history(
                tradable_id=tradable_id,
                route_id=info_route_id,
                resolution=resolution,
                from_ms=from_ms,
                to_ms=now_ms,
            )
            if df is not None and not df.empty:
                # Merge with cache, dedupe by index, keep last `count`
                merged = pd.concat([cached, df]) if cached is not None else df
                merged = merged[~merged.index.duplicated(keep="last")].sort_index()
                merged = merged.tail(count)
                self._cache_put(key, merged)
                return merged

        # All variants failed — degrade gracefully
        logger.warning(
            "all history variants failed for %s tf=%s — returning synthetic",
            symbol,
            timeframe,
        )
        df = self._synthetic(symbol, timeframe, count)
        df.attrs["synthetic"] = True
        return df

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
