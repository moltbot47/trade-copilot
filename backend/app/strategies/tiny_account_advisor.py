"""Tiny-account advisor — recommends instruments + strategy presets
that actually fit a user's broker balance.

The product's positioning is that tiny accounts ($5–$50) can still
participate in live trading because TradeLocker-backed brokers like
Genesis FX give 100:1 — 200:1 leverage on indices and majors. A user
with $5 can hold 0.01 lots of SP500 on $3.72 of initial margin and aim
for 2–10% gains per trade. The advisor is the data-driven bridge
between "I just connected my broker" and "here's what to subscribe to."

This module is pure functions over broker data + user-declared appetite.
The endpoint layer in ``app.api.tradelocker`` wraps it with the live
balance + instrument list from TradeLocker.

We do NOT call the broker's own margin endpoint per instrument — that
would be N round-trips on a list of 90+ pairs. Instead we use the static
``_MARGIN_AT_MIN_LOT_USD`` table (validated against the live demo at
build time) and flag every line in the response with
``"broker_confirms_at_order_time": True`` so the consumer knows the
broker is the source of truth at fill.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

RiskAppetite = Literal["conservative", "balanced", "aggressive"]


# Estimated initial margin per 0.01 lot, in USD, at typical Genesis FX
# leverage (200:1 on indices/FX, 20:1 on crypto). Calibrated 2026-05-11
# against the live demo (SP500 at 7,430 → $3.72 init margin at 0.01 lot).
# These are estimates only — the broker validates at order placement.
_MARGIN_AT_MIN_LOT_USD: dict[str, float] = {
    # US indices — 200:1, very capital-efficient
    "SP500": 3.72,
    "NAS100": 14.30,
    "US30": 24.90,
    # International indices
    "DE40": 12.20,
    "AU200": 4.40,
    "FTSE100": 4.80,
    "HK50": 13.40,
    "IBEX35": 8.90,
    "SWI20": 6.20,
    # Metals / commodities
    "XAUUSD": 4.30,
    "XAGUSD": 1.65,
    "WTI": 3.10,
    # FX majors — ~$1 per pip movement at 0.01 lot
    "EURUSD": 1.18,
    "GBPUSD": 1.36,
    "USDJPY": 1.05,
    "AUDUSD": 0.66,
    "NZDUSD": 0.59,
    "USDCAD": 0.74,
    "USDCHF": 0.83,
    # FX crosses
    "GBPJPY": 1.85,
    "EURGBP": 1.00,
    "EURJPY": 1.36,
    # Crypto majors — 20:1 leverage, much higher margin per 0.01 lot
    "BTCUSD": 40.30,
    "ETHUSD": 11.50,
    # Crypto alts — cheaper, but illiquid spreads
    "ADAUSD": 0.20,
    "DOGEUSD": 0.15,
    "SOLUSD": 7.20,
    "DOTUSD": 0.42,
    "LTCUSD": 2.80,
    "BCHUSD": 1.75,
    "TRXUSD": 0.18,
    "BNBUSD": 3.10,
}


# What % daily move a single 0.01 lot typically captures on a "good" day.
# Used to estimate "could a tiny account compound here?" Not a guarantee —
# pulled from a coarse rolling-volatility scan of the last 30 days.
_TYPICAL_DAILY_PCT_PER_001_LOT: dict[str, float] = {
    "SP500": 0.6,
    "NAS100": 1.0,
    "US30": 0.5,
    "DE40": 0.8,
    "AU200": 0.7,
    "FTSE100": 0.5,
    "HK50": 1.4,
    "XAUUSD": 1.1,
    "XAGUSD": 1.8,
    "WTI": 2.4,
    "EURUSD": 0.5,
    "GBPUSD": 0.6,
    "USDJPY": 0.6,
    "AUDUSD": 0.7,
    "NZDUSD": 0.7,
    "USDCAD": 0.5,
    "USDCHF": 0.5,
    "GBPJPY": 0.9,
    "EURGBP": 0.4,
    "EURJPY": 0.7,
    "BTCUSD": 2.5,
    "ETHUSD": 3.2,
    "ADAUSD": 4.0,
    "DOGEUSD": 4.5,
    "SOLUSD": 4.8,
    "DOTUSD": 4.2,
    "LTCUSD": 3.0,
    "BCHUSD": 3.5,
    "TRXUSD": 3.6,
    "BNBUSD": 2.8,
}


# Per-appetite presets. The threshold values are the LaT-PFN
# confidence-σ cutoff used by the strategy runner. Lower → more trades.
@dataclass(frozen=True)
class AppetitePreset:
    label: str
    description: str
    # LaT-PFN confidence threshold (σ above noise). 0.3 = take weak edges,
    # 1.5 = original conservative default.
    confidence_threshold: float
    # Target reward-to-risk ratio. 1.0 = aim to win as much as we risk;
    # >1 = pickier exits, <1 = scalp-friendly.
    target_rr: float
    # Max share of balance allowed in a single open position (init margin
    # / balance). Above this we warn; we never hard-block.
    max_margin_pct_per_pair: float
    # Recommended bot slugs for this appetite.
    recommended_bots: tuple[str, ...]


PRESETS: dict[str, AppetitePreset] = {
    "conservative": AppetitePreset(
        label="Conservative",
        description=(
            "High-conviction signals only (≥1.5σ). 2:1 reward-to-risk. "
            "Best for accounts ≥ $50 or users who'd rather miss a trade than take a marginal one."
        ),
        confidence_threshold=1.5,
        target_rr=2.0,
        max_margin_pct_per_pair=0.30,
        recommended_bots=("latpfn-quant",),
    ),
    "balanced": AppetitePreset(
        label="Balanced",
        description=(
            "Moderate signals (≥0.8σ). 1.5:1 reward-to-risk. The middle "
            "ground — what most users should pick first."
        ),
        confidence_threshold=0.8,
        target_rr=1.5,
        max_margin_pct_per_pair=0.50,
        recommended_bots=("latpfn-quant", "latpfn-momentum"),
    ),
    "aggressive": AppetitePreset(
        label="Aggressive",
        description=(
            "Weak edges allowed (≥0.3σ). 1:1 reward-to-risk. Compounding "
            "play for tiny accounts — many small wins per day, each ~2–5%. "
            "Higher transaction cost as a share of profit."
        ),
        confidence_threshold=0.3,
        target_rr=1.0,
        max_margin_pct_per_pair=0.75,
        recommended_bots=("latpfn-quant", "latpfn-momentum"),
    ),
}


@dataclass
class InstrumentSuggestion:
    symbol: str
    init_margin_usd: float
    max_lot_fit: float           # largest lot the balance can carry at this margin
    margin_pct_of_balance: float  # init_margin_usd / balance at min lot
    fits: bool                    # within the preset's max_margin_pct_per_pair
    warn: bool                    # tight (>50% but <max) → flag but allow
    typical_daily_pct: float      # informational
    note: str
    # The estimate caveat — keep callers honest.
    broker_confirms_at_order_time: bool = True


@dataclass
class AdvisorResponse:
    balance_usd: float
    risk_appetite: str
    preset: AppetitePreset
    suggestions: list[InstrumentSuggestion]
    skipped: list[str] = field(default_factory=list)
    # Estimated # of positions you can hold concurrently at this balance.
    # Useful for sizing the user's expectations.
    concurrent_positions_at_min_lot: int = 0


def compute_suggestions(
    balance: float,
    risk_appetite: RiskAppetite,
    broker_instrument_names: list[str],
    *,
    min_lot: float = 0.01,
) -> AdvisorResponse:
    """Build an advisor response for the given balance + appetite.

    ``broker_instrument_names`` is the list of symbols the broker offers
    (e.g., the ``name`` field from TradeLocker /instruments). We intersect
    it with our static margin table — any broker instrument we don't have
    margin data for is skipped (logged in ``response.skipped``).
    """
    preset = PRESETS.get(risk_appetite, PRESETS["balanced"])
    if balance <= 0:
        # Defensive — should never happen since the connect endpoint pulls
        # a real balance, but if it does we'd rather return an empty list
        # than divide by zero.
        return AdvisorResponse(
            balance_usd=balance,
            risk_appetite=risk_appetite,
            preset=preset,
            suggestions=[],
            skipped=list(broker_instrument_names),
        )

    broker_set = {s.strip().upper() for s in broker_instrument_names if s}
    suggestions: list[InstrumentSuggestion] = []
    skipped: list[str] = []
    smallest_margin = float("inf")

    for symbol, margin_at_min_lot in _MARGIN_AT_MIN_LOT_USD.items():
        if symbol not in broker_set:
            continue
        margin_pct = margin_at_min_lot / balance
        # max_lot is conservatively the multiple of min_lot the balance can
        # carry without breaching the preset's per-pair cap, floored at 0
        # so we never pretend an unaffordable pair has any size.
        max_lot_fit = max(
            0.0,
            min_lot * (preset.max_margin_pct_per_pair * balance / margin_at_min_lot),
        )
        fits = margin_pct <= preset.max_margin_pct_per_pair
        warn = (not fits) or (margin_pct >= preset.max_margin_pct_per_pair * 0.66)

        # Human-readable note. Order matters — most-helpful framing first.
        if not fits:
            note = (
                f"Won't fit your appetite cap ({int(preset.max_margin_pct_per_pair*100)}%) "
                f"— a single 0.01 lot needs {margin_pct*100:.0f}% of your balance."
            )
        elif warn:
            note = (
                f"Tight: a single 0.01 lot uses {margin_pct*100:.0f}% of your "
                f"balance. Leaves little room for adverse swings."
            )
        else:
            note = (
                f"Comfortable: 0.01 lot uses {margin_pct*100:.0f}% of balance. "
                f"Room for {int(1 / max(margin_pct, 0.01))} concurrent positions."
            )

        suggestions.append(
            InstrumentSuggestion(
                symbol=symbol,
                init_margin_usd=round(margin_at_min_lot, 2),
                max_lot_fit=round(max_lot_fit, 3),
                margin_pct_of_balance=round(margin_pct, 4),
                fits=fits,
                warn=warn,
                typical_daily_pct=_TYPICAL_DAILY_PCT_PER_001_LOT.get(symbol, 0.0),
                note=note,
            )
        )
        if fits and margin_at_min_lot < smallest_margin:
            smallest_margin = margin_at_min_lot

    # Broker symbols we don't have margin data for — surfaced so the
    # operator can extend the table when new instruments are added.
    for symbol in broker_set:
        if symbol not in _MARGIN_AT_MIN_LOT_USD:
            skipped.append(symbol)

    # Rank: fits first, then by margin-as-share-of-balance ascending (cheapest first).
    suggestions.sort(key=lambda s: (not s.fits, s.margin_pct_of_balance))

    concurrent = int(1 / max(smallest_margin / balance, 0.01)) if suggestions else 0

    return AdvisorResponse(
        balance_usd=round(balance, 2),
        risk_appetite=risk_appetite,
        preset=preset,
        suggestions=suggestions,
        skipped=sorted(skipped),
        concurrent_positions_at_min_lot=concurrent,
    )
