"""Reusable compound-growth strategy PDF generator.

Used by the public /api/calculator/generate endpoint. Takes user-supplied
inputs and returns PDF bytes. Tightly coupled to the "extremely low-risk
LaT-PFN Momentum" framing on the strategy page.
"""
from __future__ import annotations

import io
from datetime import datetime

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT
from reportlab.lib.pagesizes import LETTER, landscape
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import (
    PageBreak,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)


INK = colors.HexColor("#0E1116")
ACCENT = colors.HexColor("#0A6B3F")
ACCENT_LIGHT = colors.HexColor("#E8F5EE")
ROW_ALT = colors.HexColor("#F4F6F8")
MUTED = colors.HexColor("#6B7280")
WARN = colors.HexColor("#B45309")
HEADER_BG = colors.HexColor("#0E1116")


def _build_series(start: float, rate: float, days: int) -> list[float]:
    out = [start]
    for _ in range(days):
        out.append(out[-1] * (1.0 + rate))
    return out


def _fmt_usd(v: float) -> str:
    return f"${v:,.2f}"


def _fmt_x(v: float, base: float) -> str:
    return f"{v / base:.2f}×"


def generate_compound_pdf(
    start_balance: float = 10_000.0,
    daily_rate_pct: float = 2.0,
    days: int = 100,
    requester_label: str | None = None,
) -> bytes:
    """Build a personalized compound strategy PDF and return its bytes.

    Args:
        start_balance: starting account balance in USD (must be > 0)
        daily_rate_pct: daily profit target as a percentage (e.g. 2.0 = 2%)
        days: forecast horizon (e.g. 100)
        requester_label: optional name shown on the cover (e.g. user email)
    """
    start_balance = max(100.0, min(float(start_balance), 10_000_000.0))
    daily_rate_pct = max(0.1, min(float(daily_rate_pct), 10.0))
    days = max(7, min(int(days), 365))

    # Always show three scenarios anchored on the user's chosen rate
    user_rate = daily_rate_pct / 100.0
    rates = sorted({
        round(max(0.001, user_rate - 0.01), 4),
        round(user_rate, 4),
        round(min(0.10, user_rate + 0.01), 4),
    })
    series = {f"{r * 100:.1f}%": _build_series(start_balance, r, days) for r in rates}

    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf,
        pagesize=landscape(LETTER),
        leftMargin=0.5 * inch,
        rightMargin=0.5 * inch,
        topMargin=0.6 * inch,
        bottomMargin=0.45 * inch,
        title="Compound Growth Strategy — Trade Copilot",
        author="Trade Copilot",
    )

    styles = getSampleStyleSheet()
    title = ParagraphStyle("T", parent=styles["Title"], fontName="Helvetica-Bold",
                           fontSize=24, leading=30, textColor=INK,
                           alignment=TA_LEFT, spaceBefore=2, spaceAfter=4)
    subtitle = ParagraphStyle("ST", parent=styles["Normal"], fontName="Helvetica",
                              fontSize=11, leading=15, textColor=MUTED, alignment=TA_LEFT, spaceAfter=14)
    sect = ParagraphStyle("SE", parent=styles["Heading2"], fontName="Helvetica-Bold",
                          fontSize=13, leading=17, textColor=ACCENT, spaceBefore=8, spaceAfter=4)
    body = ParagraphStyle("B", parent=styles["Normal"], fontName="Helvetica",
                          fontSize=9.5, leading=13, textColor=INK)
    # Card-specific styles — fix the overlap-with-subtitle bug.
    card_label = ParagraphStyle("CL", parent=body, fontSize=10, leading=13,
                                spaceAfter=4)
    card_big = ParagraphStyle("CB", parent=body, fontName="Helvetica-Bold",
                              fontSize=20, leading=26, textColor=ACCENT,
                              spaceBefore=2, spaceAfter=4)
    card_sub = ParagraphStyle("CS", parent=body, fontSize=9, leading=12,
                              textColor=MUTED, spaceBefore=2)
    foot = ParagraphStyle("F", parent=styles["Normal"], fontName="Helvetica-Oblique",
                          fontSize=8, leading=11, textColor=MUTED, alignment=TA_CENTER)

    story = []

    # Cover
    story.append(Paragraph("Your Compound Growth Plan", title))
    sub_text = (
        f"Starting balance: <b>{_fmt_usd(start_balance)}</b> · "
        f"Target rate: <b>{daily_rate_pct:.2f}%/day</b> · "
        f"Horizon: <b>{days} days</b>"
    )
    if requester_label:
        sub_text += f" · Prepared for: <b>{requester_label}</b>"
    sub_text += f" · Generated: {datetime.utcnow():%Y-%m-%d %H:%M UTC}"
    story.append(Paragraph(sub_text, subtitle))

    # Strategy attribution callout
    story.append(Paragraph("Powered by the LaT-PFN Momentum Strategy", sect))
    story.append(Paragraph(
        "The projection below assumes the trader executes our <b>extremely low-risk "
        "LaT-PFN Momentum strategy</b>: ML-forecast confidence-gated entries, ATR-sized "
        "stops, hedging-mode position management on Genesis FX TradeLocker, and a "
        "self-tuning feedback loop that auto-pauses the bot when rolling drawdown exceeds "
        "10%. The strategy targets 1–2% daily returns by taking many small, statistically "
        "favorable trades rather than swinging for big wins.",
        body,
    ))
    story.append(Spacer(1, 12))

    # Three scenario cards
    rate_keys = sorted(series.keys(), key=lambda k: float(k.rstrip("%")))
    cards = []
    for k in rate_keys:
        r_val = float(k.rstrip("%"))
        end = series[k][-1]
        if r_val < daily_rate_pct - 0.5:
            tier = "Conservative"
        elif r_val > daily_rate_pct + 0.5:
            tier = "Aggressive"
        else:
            tier = "Your Target"
        cards.append([
            Paragraph(f"<b>{tier} · {k}/day</b>", card_label),
            Paragraph(f"{_fmt_usd(end)}", card_big),
            Paragraph(
                f"after {days} days · {_fmt_x(end, start_balance)}",
                card_sub,
            ),
        ])

    # Sized to fit landscape-letter usable width (~10.0") with 0.5" margins.
    summary_table = Table(
        [[c for c in cards]],
        colWidths=[3.3 * inch] * len(cards),
    )
    summary_table.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("BACKGROUND", (0, 0), (-1, -1), ACCENT_LIGHT),
        ("BOX", (0, 0), (0, 0), 1, ACCENT),
        ("BOX", (1, 0), (1, 0), 1, ACCENT),
        ("BOX", (2, 0), (2, 0), 1, ACCENT) if len(cards) > 2 else ("BOX", (0, 0), (-1, -1), 1, ACCENT),
        ("LEFTPADDING", (0, 0), (-1, -1), 12),
        ("RIGHTPADDING", (0, 0), (-1, -1), 12),
        ("TOPPADDING", (0, 0), (-1, -1), 14),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 14),
    ]))
    story.append(summary_table)
    story.append(Spacer(1, 14))

    # Milestones
    story.append(Paragraph("Key Milestones", sect))
    milestone_days = sorted({
        max(1, days // 10),
        max(1, days // 4),
        max(1, days // 2),
        max(1, days * 3 // 4),
        days,
    })
    ms_data = [["Day"] + [f"{k}/day" for k in rate_keys]]
    for d in milestone_days:
        row = [str(d)]
        for k in rate_keys:
            v = series[k][d]
            row.append(f"{_fmt_usd(v)}  ({_fmt_x(v, start_balance)})")
        ms_data.append(row)
    ms_t = Table(ms_data, colWidths=[0.7 * inch] + [3.0 * inch] * len(rate_keys))
    ms_t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), HEADER_BG),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 10),
        ("ALIGN", (0, 0), (0, -1), "CENTER"),
        ("ALIGN", (1, 0), (-1, -1), "LEFT"),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, ROW_ALT]),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#D1D5DB")),
        ("LEFTPADDING", (0, 0), (-1, -1), 8),
        ("RIGHTPADDING", (0, 0), (-1, -1), 8),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
    ]))
    story.append(ms_t)

    story.append(PageBreak())

    # Daily breakdown — multi-column
    story.append(Paragraph(f"Daily Breakdown — {days} days", title))
    story.append(Paragraph(
        f"All {days} days laid out across columns for compact viewing.",
        subtitle,
    ))

    col_count = 4
    days_per_col = (days + col_count - 1) // col_count
    # Landscape-letter usable width is ~10.0" after 0.5" L/R margins.
    # Each sub-table must fit 10.0/4 = 2.5" wide minus a tiny gap.
    n_rate_cols = len(rate_keys)
    DAY_W = 0.40 * inch
    INNER_W = 2.42 * inch  # max width per sub-table content
    rate_col_w = (INNER_W - DAY_W) / max(n_rate_cols, 1)
    SUB_TABLE_W = DAY_W + rate_col_w * n_rate_cols  # ~2.42 inches
    sub_tables = []
    for col_idx in range(col_count):
        start_day = col_idx * days_per_col + 1
        end_day = min(start_day + days_per_col - 1, days)
        if start_day > days:
            sub_tables.append("")
            continue
        rows = [["Day"] + [k for k in rate_keys]]
        for d in range(start_day, end_day + 1):
            rows.append([str(d)] + [_fmt_usd(series[k][d]) for k in rate_keys])
        col_widths = [DAY_W] + [rate_col_w] * n_rate_cols
        t = Table(rows, colWidths=col_widths)
        style_cmds = [
            ("BACKGROUND", (0, 0), (-1, 0), HEADER_BG),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("FONTSIZE", (0, 0), (-1, -1), 7),
            ("ALIGN", (0, 0), (0, -1), "CENTER"),
            ("ALIGN", (1, 0), (-1, -1), "RIGHT"),
            ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#E5E7EB")),
            ("TOPPADDING", (0, 0), (-1, -1), 1.5),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 1.5),
            ("LEFTPADDING", (0, 0), (-1, -1), 4),
            ("RIGHTPADDING", (0, 0), (-1, -1), 4),
        ]
        for ridx in range(1, len(rows)):
            actual_day = start_day + ridx - 1
            if actual_day % 5 == 0:
                style_cmds.append(("BACKGROUND", (0, ridx), (-1, ridx), ACCENT_LIGHT))
                style_cmds.append(("FONTNAME", (0, ridx), (-1, ridx), "Helvetica-Bold"))
            elif ridx % 2 == 0:
                style_cmds.append(("BACKGROUND", (0, ridx), (-1, ridx), ROW_ALT))
        t.setStyle(TableStyle(style_cmds))
        sub_tables.append(t)

    # Outer grid — each cell holds one sub-table; total 4 × 2.5" = 10.0"
    grid = Table([sub_tables], colWidths=[SUB_TABLE_W + 0.08 * inch] * col_count)
    grid.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 0),
    ]))
    story.append(grid)

    story.append(PageBreak())

    # Strategy + risk page
    story.append(Paragraph("Why This Strategy is Considered Low-Risk", title))
    story.append(Paragraph("Built around four hard risk controls.", subtitle))

    story.append(Paragraph("1 · Confidence-gated entries", sect))
    story.append(Paragraph(
        "The LaT-PFN forecasting model returns a directional prediction with explicit "
        "uncertainty (σ). The strategy only fires when the predicted move is at least "
        "<b>1.5σ</b> larger than forecast noise — most signals are filtered out. "
        "This avoids low-conviction entries that account for most retail losses.",
        body,
    ))

    story.append(Paragraph("2 · ATR-sized stops, never wider", sect))
    story.append(Paragraph(
        "Every position is opened with a hard stop at <b>1× ATR(14)</b> against the entry. "
        "Worst-case loss per trade is mathematically capped before the order is placed. "
        "Position size scales down so that one stop-out costs ~<b>1% of account</b> by default.",
        body,
    ))

    story.append(Paragraph("3 · Auto-pause on drawdown", sect))
    story.append(Paragraph(
        "The self-tuning feedback loop monitors rolling 20-trade performance. If drawdown "
        "exceeds 10% or win-rate drops below 45%, the bot <b>auto-pauses for 30 minutes</b> "
        "and tightens its confidence threshold. Bad streaks can't run away with the account.",
        body,
    ))

    story.append(Paragraph("4 · Hedging-mode position isolation", sect))
    story.append(Paragraph(
        "Each open trade is its own isolated position on Genesis FX TradeLocker. Closing one "
        "doesn't accidentally net against another. You always know your exposure to the cent.",
        body,
    ))

    story.append(Spacer(1, 14))

    # Disclaimer
    disclaimer_data = [[
        Paragraph(
            "<b>Disclaimer:</b> This document is illustrative and educational only. "
            "Compound projections show what is mathematically possible if a daily target "
            "is hit consistently — they are not guarantees, predictions, or solicitations. "
            "Past performance does not indicate future results. Trading involves substantial "
            "risk of loss including total loss of capital. Trade Copilot is donation-supported "
            "open-source software and does not provide investment advice. Consult a licensed "
            "advisor before making financial decisions.",
            ParagraphStyle("d", parent=body, fontSize=8.5, textColor=WARN),
        )
    ]]
    disclaimer_t = Table(disclaimer_data, colWidths=[10 * inch])
    disclaimer_t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#FEF3C7")),
        ("BOX", (0, 0), (-1, -1), 0.75, WARN),
        ("LEFTPADDING", (0, 0), (-1, -1), 12),
        ("RIGHTPADDING", (0, 0), (-1, -1), 12),
        ("TOPPADDING", (0, 0), (-1, -1), 8),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
    ]))
    story.append(disclaimer_t)

    story.append(Spacer(1, 18))
    story.append(Paragraph(
        "Trade Copilot · buymeacoffee.com/dbutler · Educational use only",
        foot,
    ))

    doc.build(story)
    return buf.getvalue()
