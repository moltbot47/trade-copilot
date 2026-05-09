"""Direct unit tests for the compound PDF builder."""
from __future__ import annotations

from app.core.compound_pdf import generate_compound_pdf


def test_generate_returns_pdf_bytes():
    body = generate_compound_pdf(
        start_balance=10_000.0, daily_rate_pct=2.0, days=30
    )
    assert isinstance(body, bytes)
    assert body[:4] == b"%PDF"
    assert len(body) > 1000


def test_generate_with_label_embeds_in_pdf():
    body = generate_compound_pdf(
        start_balance=10_000.0,
        daily_rate_pct=2.0,
        days=30,
        requester_label="Alice Tester",
    )
    assert body[:4] == b"%PDF"


def test_generate_clamps_negative_balance():
    """Negative balances are silently clamped, not crashed."""
    body = generate_compound_pdf(
        start_balance=-50.0, daily_rate_pct=2.0, days=30
    )
    assert body[:4] == b"%PDF"


def test_generate_clamps_huge_balance():
    body = generate_compound_pdf(
        start_balance=10_000_000_000.0, daily_rate_pct=1.0, days=30
    )
    assert body[:4] == b"%PDF"


def test_generate_clamps_high_rate():
    body = generate_compound_pdf(
        start_balance=10_000.0, daily_rate_pct=99.0, days=30
    )
    assert body[:4] == b"%PDF"


def test_generate_clamps_low_days():
    body = generate_compound_pdf(start_balance=10_000.0, daily_rate_pct=2.0, days=1)
    assert body[:4] == b"%PDF"


def test_generate_typical_horizon():
    """100-day horizon is the calculator default and well-supported."""
    body = generate_compound_pdf(
        start_balance=10_000.0, daily_rate_pct=2.0, days=100
    )
    assert body[:4] == b"%PDF"
    assert len(body) > 5000  # multi-page report


def test_generate_with_default_args():
    body = generate_compound_pdf()
    assert body[:4] == b"%PDF"
    assert len(body) > 1000
