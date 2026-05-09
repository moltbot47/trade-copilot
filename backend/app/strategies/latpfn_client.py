"""LaT-PFN forecaster client.

For now this defaults to a deterministic mock that linearly extrapolates the
last 16 closes and adds jitter for the std band. The real LaT-PFN model is
slow on CPU (~1.5–4s per forecast on this Intel Mac) so production deploys
will run it on RunPod / DigitalOcean GPU and expose it as an HTTP service.
This client speaks both: pass `endpoint_url` to use the real service, leave
it None (or set env LATPFN_MOCK=1) to use the mock.

Real endpoint contract (planned):
    POST {endpoint_url}/forecast
    body: {"closes": [float, ...], "n_predict": int}
    resp: {"mean": [float, ...], "std": [float, ...]}
"""
from __future__ import annotations

import logging
import os
from typing import Optional

import httpx
import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


class LaTPFNClient:
    """Forecaster client. Same shape whether running real or mock."""

    def __init__(
        self,
        endpoint_url: Optional[str] = None,
        timeout: float = 30.0,
        context_window: int = 16,
    ) -> None:
        self.endpoint_url = endpoint_url
        self.timeout = timeout
        self.context_window = context_window

    @property
    def is_mock(self) -> bool:
        return self.endpoint_url is None or os.getenv("LATPFN_MOCK", "") == "1"

    async def forecast(
        self,
        bars: pd.DataFrame,
        n_predict: int = 12,
    ) -> dict:
        """Return forecast {mean, std, current_price}.

        mean and std are arrays of length n_predict, in the same units as
        bars["close"].
        """
        if "close" not in bars.columns or len(bars) < 2:
            raise ValueError("bars must have a 'close' column with >= 2 rows")
        closes = bars["close"].astype(float).to_numpy()
        current = float(closes[-1])

        if self.is_mock:
            mean, std = self._mock_forecast(closes, n_predict)
            return {
                "mean": mean.tolist(),
                "std": std.tolist(),
                "current_price": current,
            }

        # Real HTTP call
        payload = {"closes": closes.tolist(), "n_predict": int(n_predict)}
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as c:
                r = await c.post(f"{self.endpoint_url.rstrip('/')}/forecast", json=payload)
                r.raise_for_status()
                data = r.json()
        except httpx.HTTPError as exc:
            logger.warning("LaT-PFN remote forecast failed (%s) — falling back to mock", exc)
            mean, std = self._mock_forecast(closes, n_predict)
            return {
                "mean": mean.tolist(),
                "std": std.tolist(),
                "current_price": current,
                "fallback": True,
            }

        mean = np.asarray(data["mean"], dtype=float)
        std = np.asarray(data["std"], dtype=float)
        return {"mean": mean.tolist(), "std": std.tolist(), "current_price": current}

    def _mock_forecast(
        self, closes: np.ndarray, n_predict: int
    ) -> tuple[np.ndarray, np.ndarray]:
        """Deterministic linear-regression extrapolation with jittered std.

        Same input → same output (seeded by closes tuple).
        """
        ctx = closes[-self.context_window :] if len(closes) >= self.context_window else closes
        x = np.arange(len(ctx), dtype=float)
        # Linear fit
        if len(ctx) >= 2:
            slope, intercept = np.polyfit(x, ctx, 1)
        else:
            slope, intercept = 0.0, float(ctx[-1])
        # Realized noise around fit → use as base std
        residual = ctx - (slope * x + intercept)
        base_std = float(np.std(residual)) if len(ctx) > 1 else max(abs(ctx[-1]) * 0.001, 1e-6)
        if base_std <= 0:
            base_std = max(abs(ctx[-1]) * 0.001, 1e-6)

        seed = abs(hash(tuple(np.round(ctx, 6).tolist()))) % (2**32)
        rng = np.random.RandomState(seed)

        future_x = np.arange(len(ctx), len(ctx) + n_predict, dtype=float)
        mean = slope * future_x + intercept
        # Add small deterministic jitter so mean isn't a perfect line
        mean = mean + rng.normal(0, base_std * 0.1, size=n_predict)
        # Std grows ~ sqrt(h) with horizon (forecast uncertainty compounds)
        horizons = np.arange(1, n_predict + 1)
        std = base_std * np.sqrt(horizons) * (1.0 + rng.uniform(-0.05, 0.05, size=n_predict))
        std = np.maximum(std, 1e-6)
        return mean, std
