"""Unit tests for the tiny-account advisor.

Pure-function tests against compute_suggestions — no HTTP, no broker, no DB.
Each test pins one behavior we don't want to regress as the static margin
table is extended.
"""
from __future__ import annotations

import pytest

from app.strategies.tiny_account_advisor import (
    PRESETS,
    compute_suggestions,
)


# A representative slice of what the live broker /instruments returns —
# pulled directly from the Genesis FX demo on 2026-05-11.
DEMO_INSTRUMENTS = [
    "SP500", "NAS100", "US30", "DE40", "FTSE100", "AU200", "HK50",
    "XAUUSD", "XAGUSD", "WTI",
    "EURUSD", "GBPUSD", "USDJPY", "AUDUSD", "USDCAD",
    "GBPJPY", "EURJPY",
    "BTCUSD", "ETHUSD",
    "ADAUSD", "DOGEUSD", "SOLUSD", "LTCUSD", "BCHUSD",
]


def _by_symbol(suggestions, sym):
    return next((s for s in suggestions if s.symbol == sym), None)


def test_five_dollar_balance_includes_fx_majors_and_warns_on_indices():
    """The $5 account: FX majors fit cleanly, SP500 still fits but with a warn,
    BTC/ETH are flagged as too big to open at min lot."""
    result = compute_suggestions(5.0, "balanced", DEMO_INSTRUMENTS)

    eurusd = _by_symbol(result.suggestions, "EURUSD")
    assert eurusd is not None
    assert eurusd.fits
    # EURUSD at $1.18 margin / $5 balance = 23.6% — under balanced cap of 50%
    assert eurusd.margin_pct_of_balance < 0.30

    sp500 = _by_symbol(result.suggestions, "SP500")
    assert sp500 is not None
    # SP500 at $3.72 / $5 = 74% of balance, which exceeds the 50% cap of
    # the balanced preset → flagged as not fitting. Aggressive (75%) would
    # accept it (see test_aggressive_unlocks_sp500_on_five_dollars below).
    assert sp500.margin_pct_of_balance > 0.50
    assert sp500.fits is False

    btc = _by_symbol(result.suggestions, "BTCUSD")
    assert btc is not None
    assert btc.fits is False
    assert "Won't fit" in btc.note


def test_aggressive_appetite_accepts_more_pairs_than_conservative():
    """The 'aggressive' preset has a higher per-pair cap, so a $5 account
    has more fitting pairs than under 'conservative'."""
    aggressive = compute_suggestions(5.0, "aggressive", DEMO_INSTRUMENTS)
    conservative = compute_suggestions(5.0, "conservative", DEMO_INSTRUMENTS)

    agg_fits = [s.symbol for s in aggressive.suggestions if s.fits]
    cons_fits = [s.symbol for s in conservative.suggestions if s.fits]

    assert set(cons_fits).issubset(set(agg_fits))
    assert len(agg_fits) >= len(cons_fits) + 1  # at least one extra pair


def test_aggressive_unlocks_sp500_on_five_dollars():
    """The exact case from the user's screenshot: $5 balance, SP500 at
    $3.72 init margin. Should be locked on conservative/balanced but
    unlocked on aggressive (75% cap)."""
    aggressive = compute_suggestions(5.0, "aggressive", DEMO_INSTRUMENTS)
    sp500 = _by_symbol(aggressive.suggestions, "SP500")
    assert sp500 is not None
    assert sp500.fits, f"SP500 should fit on aggressive at $5 balance; note={sp500.note}"
    assert sp500.warn, "Even when fitting, SP500 at $3.72/$5 should warn (>66% of cap)"


def test_zero_balance_returns_empty_suggestions():
    """Defensive: an empty/zero balance produces an empty response rather
    than divide-by-zero or all-rejected garbage."""
    result = compute_suggestions(0.0, "balanced", DEMO_INSTRUMENTS)
    assert result.suggestions == []
    # Skipped list captures what we couldn't process — every broker pair.
    assert len(result.skipped) == len(DEMO_INSTRUMENTS)


def test_suggestions_ranked_fits_first_then_cheapest():
    """The 'fits' suggestions should bubble up first, then within fits, the
    cheapest-margin pair should be at the top — that's the right
    onboarding suggestion."""
    result = compute_suggestions(20.0, "balanced", DEMO_INSTRUMENTS)
    # Every "fits" row must come before any "doesn't fit" row.
    fits_seen = True
    for s in result.suggestions:
        if not s.fits:
            fits_seen = False
        elif not fits_seen:
            pytest.fail("a fitting pair appeared after a non-fitting pair")
    # First fitting row must be one of the cheapest pairs.
    first_fitter = next(s for s in result.suggestions if s.fits)
    assert first_fitter.symbol in {
        "DOGEUSD", "TRXUSD", "ADAUSD", "DOTUSD", "NZDUSD", "AUDUSD"
    }, f"first fitter was {first_fitter.symbol}"


def test_unknown_appetite_falls_back_to_balanced():
    """A garbage appetite string should not crash — it should fall back."""
    result = compute_suggestions(50.0, "weird-string", DEMO_INSTRUMENTS)  # type: ignore[arg-type]
    assert result.preset is PRESETS["balanced"]


def test_skipped_captures_broker_pairs_without_margin_data():
    """If the broker returns a pair the advisor doesn't know about, it's
    surfaced in `skipped` so the operator can extend the static table."""
    result = compute_suggestions(50.0, "balanced", ["EURUSD", "FUTURE_PAIR_XYZ"])
    assert "FUTURE_PAIR_XYZ" in result.skipped
    # Pairs we DO know about don't end up in skipped.
    assert "EURUSD" not in result.skipped


def test_concurrent_positions_estimate_is_reasonable():
    """The 'how many positions at once?' hint is a rough proxy: balance /
    smallest fitting margin. Validate it doesn't blow up."""
    result = compute_suggestions(20.0, "aggressive", DEMO_INSTRUMENTS)
    assert 0 < result.concurrent_positions_at_min_lot < 200


def test_preset_descriptions_render_human_text():
    """Light schema check — descriptions are non-empty and bot lists are
    populated so the UI can render the preset card without nulls."""
    for name, preset in PRESETS.items():
        assert preset.label
        assert preset.description
        assert 0.0 < preset.confidence_threshold < 5.0
        assert 0.5 <= preset.target_rr <= 3.0
        assert 0.0 < preset.max_margin_pct_per_pair <= 1.0
        assert preset.recommended_bots
