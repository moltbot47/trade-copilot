"""Additional edge-case tests for compound_pdf.generate_compound_pdf."""
from __future__ import annotations

from app.core.compound_pdf import generate_compound_pdf


def test_pdf_starts_with_pdf_magic_bytes():
    body = generate_compound_pdf(start_balance=10_000.0, daily_rate_pct=2.0, days=30)
    assert body.startswith(b"%PDF-")


def test_pdf_size_in_expected_range_for_default_inputs():
    body = generate_compound_pdf()
    # Default 2%/day, 100 days → multi-page report with day breakdown grid.
    # Empirically ~20-200 KB; allow generous range.
    assert 5_000 < len(body) < 500_000


def test_custom_requester_label_produces_distinct_output():
    """Two PDFs with different labels should differ in size/content."""
    a = generate_compound_pdf(
        start_balance=10_000.0, daily_rate_pct=2.0, days=30, requester_label="Alice"
    )
    b = generate_compound_pdf(
        start_balance=10_000.0, daily_rate_pct=2.0, days=30, requester_label="Robert J. Smithington"
    )
    # Different label string lengths → different byte streams.
    assert a != b


def test_100_day_vs_7_day_produces_different_sizes():
    a = generate_compound_pdf(start_balance=10_000.0, daily_rate_pct=2.0, days=7)
    b = generate_compound_pdf(start_balance=10_000.0, daily_rate_pct=2.0, days=100)
    assert len(a) != len(b)
    # 100-day breakdown definitely larger than 7-day
    assert len(b) > len(a)


def test_lower_bound_rate_still_produces_valid_pdf():
    body = generate_compound_pdf(start_balance=10_000.0, daily_rate_pct=0.1, days=30)
    assert body.startswith(b"%PDF-")
    assert len(body) > 1000


def test_upper_bound_rate_still_produces_valid_pdf():
    """daily_rate_pct=10.0 is the documented clamp ceiling."""
    body = generate_compound_pdf(start_balance=10_000.0, daily_rate_pct=10.0, days=30)
    assert body.startswith(b"%PDF-")
    assert len(body) > 1000


def test_100_day_horizon_does_not_crash():
    """Regression: Wave 1B reported LayoutError for ≥200 days; 100 should be safe."""
    body = generate_compound_pdf(start_balance=10_000.0, daily_rate_pct=2.0, days=100)
    assert body.startswith(b"%PDF-")
    assert len(body) > 5000


def test_minimum_balance_clamped_does_not_crash():
    body = generate_compound_pdf(start_balance=1.0, daily_rate_pct=1.0, days=20)
    assert body.startswith(b"%PDF-")


def test_extreme_combo_low_rate_long_horizon():
    """0.1%/day over 90 days — very low compound. Should still render."""
    body = generate_compound_pdf(start_balance=5_000.0, daily_rate_pct=0.1, days=90)
    assert body.startswith(b"%PDF-")
    assert len(body) > 1000


def test_pdf_ends_with_eof_marker():
    body = generate_compound_pdf(start_balance=10_000.0, daily_rate_pct=2.0, days=30)
    # Valid PDFs end with %%EOF (possibly followed by a newline).
    assert b"%%EOF" in body[-32:]
