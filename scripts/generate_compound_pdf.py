"""Generate a compound-growth strategy PDF for Trade Copilot.

Shows how a starting account grows under 1%, 2%, and 3% daily compounding
over a 100-day period. Multi-column layout fits all 100 days on one page.
"""
from __future__ import annotations

from pathlib import Path

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

# ---------- Configuration ----------
START_BALANCE = 10_000.00
DAYS = 100
RATES = [0.01, 0.02, 0.03]  # 1%, 2%, 3% daily

OUT = Path("/Users/mac/trade-copilot/docs/compound_strategy.pdf")
OUT.parent.mkdir(parents=True, exist_ok=True)

# Brand colors (Trade Copilot terminal/TUI palette → professional print version)
INK = colors.HexColor("#0E1116")
ACCENT = colors.HexColor("#0A6B3F")  # deep green for printed PDF
ACCENT_LIGHT = colors.HexColor("#E8F5EE")
ROW_ALT = colors.HexColor("#F4F6F8")
MUTED = colors.HexColor("#6B7280")
WARN = colors.HexColor("#B45309")
HEADER_BG = colors.HexColor("#0E1116")


# ---------- Math ----------
def build_table(rate: float) -> list[float]:
    """Day 0..100 cumulative balance under daily compound at `rate`."""
    out = [START_BALANCE]
    for _ in range(DAYS):
        out.append(out[-1] * (1.0 + rate))
    return out


def fmt_usd(v: float) -> str:
    return f"${v:,.2f}"


def fmt_x(v: float) -> str:
    return f"{v / START_BALANCE:.2f}×"


# ---------- Document ----------
def build_pdf() -> Path:
    doc = SimpleDocTemplate(
        str(OUT),
        pagesize=landscape(LETTER),
        leftMargin=0.4 * inch,
        rightMargin=0.4 * inch,
        topMargin=0.45 * inch,
        bottomMargin=0.35 * inch,
        title="Compound Growth Strategy — Trade Copilot",
        author="Trade Copilot",
    )

    styles = getSampleStyleSheet()
    title = ParagraphStyle(
        "T", parent=styles["Title"],
        fontName="Helvetica-Bold", fontSize=24, leading=28, textColor=INK,
        alignment=TA_LEFT, spaceAfter=4,
    )
    subtitle = ParagraphStyle(
        "ST", parent=styles["Normal"],
        fontName="Helvetica", fontSize=11, leading=14, textColor=MUTED,
        alignment=TA_LEFT, spaceAfter=14,
    )
    sect = ParagraphStyle(
        "SE", parent=styles["Heading2"],
        fontName="Helvetica-Bold", fontSize=13, textColor=ACCENT,
        spaceBefore=8, spaceAfter=4,
    )
    body = ParagraphStyle(
        "B", parent=styles["Normal"],
        fontName="Helvetica", fontSize=9.5, leading=13, textColor=INK,
    )
    foot = ParagraphStyle(
        "F", parent=styles["Normal"],
        fontName="Helvetica-Oblique", fontSize=8, leading=11,
        textColor=MUTED, alignment=TA_CENTER,
    )

    story = []

    # ===== Page 1 — Cover & Summary =====
    story.append(Paragraph("Compound Growth Strategy", title))
    story.append(Paragraph(
        f"Daily profit goals of 1%, 2%, and 3% on a starting balance of "
        f"<b>{fmt_usd(START_BALANCE)}</b> compounded over <b>{DAYS} days</b>.",
        subtitle,
    ))

    # Series
    series = {f"{int(r*100)}%": build_table(r) for r in RATES}

    # Summary cards row (3 cards)
    summary_cards = []
    card_specs = [
        ("Conservative · 1%/day", "01", series["1%"], "Safer pace, smaller per-trade risk"),
        ("Moderate · 2%/day", "02", series["2%"], "Balanced pace, the typical edge target"),
        ("Aggressive · 3%/day", "03", series["3%"], "Higher variance, requires tight discipline"),
    ]
    for tier, _, vals, desc in card_specs:
        end_val = vals[-1]
        summary_cards.append([[
            Paragraph(f"<b>{tier}</b>", body),
            Paragraph(
                f"<font size=18 color='#0A6B3F'><b>{fmt_usd(end_val)}</b></font>",
                body,
            ),
            Paragraph(f"<font color='#6B7280'>after {DAYS} days · {fmt_x(end_val)}</font>", body),
            Spacer(1, 4),
            Paragraph(f"<i>{desc}</i>", body),
        ]])

    summary_table = Table(
        [[summary_cards[0][0], summary_cards[1][0], summary_cards[2][0]]],
        colWidths=[3.4 * inch, 3.4 * inch, 3.4 * inch],
    )
    summary_table.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("BOX", (0, 0), (0, 0), 1, ACCENT),
        ("BOX", (1, 0), (1, 0), 1, ACCENT),
        ("BOX", (2, 0), (2, 0), 1, ACCENT),
        ("BACKGROUND", (0, 0), (-1, -1), ACCENT_LIGHT),
        ("LEFTPADDING", (0, 0), (-1, -1), 12),
        ("RIGHTPADDING", (0, 0), (-1, -1), 12),
        ("TOPPADDING", (0, 0), (-1, -1), 14),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 14),
    ]))
    story.append(summary_table)
    story.append(Spacer(1, 14))

    # Milestones table
    story.append(Paragraph("Key Milestones", sect))
    milestones = [10, 25, 50, 75, 100]
    ms_data = [["Day", "1% daily", "2% daily", "3% daily"]]
    for d in milestones:
        ms_data.append([
            str(d),
            f"{fmt_usd(series['1%'][d])}  ({fmt_x(series['1%'][d])})",
            f"{fmt_usd(series['2%'][d])}  ({fmt_x(series['2%'][d])})",
            f"{fmt_usd(series['3%'][d])}  ({fmt_x(series['3%'][d])})",
        ])
    ms_t = Table(ms_data, colWidths=[0.7 * inch, 3.0 * inch, 3.0 * inch, 3.0 * inch])
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

    story.append(Spacer(1, 16))
    story.append(Paragraph(
        "<b>How to read this:</b> Each rate compounds the prior day's balance. "
        "A 2%/day target means each day you aim to grow your account by 2% of yesterday's "
        "ending balance. Targets are <i>profit goals</i>, not guarantees — actual results "
        "depend on win rate, risk per trade, and market conditions.",
        body,
    ))

    story.append(PageBreak())

    # ===== Page 2 — 100-day daily breakdown in MULTI-COLUMN layout =====
    story.append(Paragraph(
        f"100-Day Daily Breakdown — {fmt_usd(START_BALANCE)} starting balance",
        title,
    ))
    story.append(Paragraph(
        "Days 1–100 split across four columns. Each cell shows the end-of-day balance "
        "under each compounding rate.",
        subtitle,
    ))

    # Build 4 columns × 25 days each. Each column has its own subtable.
    col_count = 4
    days_per_col = DAYS // col_count  # 25
    sub_tables = []
    for col_idx in range(col_count):
        start_day = col_idx * days_per_col + 1
        end_day = start_day + days_per_col - 1
        rows = [["Day", "1%", "2%", "3%"]]
        for d in range(start_day, end_day + 1):
            rows.append([
                str(d),
                fmt_usd(series["1%"][d]),
                fmt_usd(series["2%"][d]),
                fmt_usd(series["3%"][d]),
            ])
        t = Table(rows, colWidths=[0.4 * inch, 0.85 * inch, 0.95 * inch, 1.05 * inch])
        # Highlight every 5th day
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
        # zebra-stripe & milestone highlight
        for ridx in range(1, len(rows)):
            actual_day = start_day + ridx - 1
            if actual_day % 5 == 0:
                style_cmds.append(("BACKGROUND", (0, ridx), (-1, ridx), ACCENT_LIGHT))
                style_cmds.append(("FONTNAME", (0, ridx), (-1, ridx), "Helvetica-Bold"))
            elif ridx % 2 == 0:
                style_cmds.append(("BACKGROUND", (0, ridx), (-1, ridx), ROW_ALT))
        t.setStyle(TableStyle(style_cmds))
        sub_tables.append(t)

    # Compose the 4 sub-tables in a single row
    grid = Table([sub_tables], colWidths=[3.3 * inch] * col_count)
    grid.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 4),
        ("RIGHTPADDING", (0, 0), (-1, -1), 4),
    ]))
    story.append(grid)

    story.append(Spacer(1, 8))
    story.append(Paragraph(
        "<font color='#0A6B3F'><b>■</b></font> Highlighted rows = every 5th day milestone.",
        ParagraphStyle("legend", parent=body, fontSize=8, textColor=MUTED),
    ))

    story.append(PageBreak())

    # ===== Page 3 — Risk & realism notes =====
    story.append(Paragraph("How This Works in Practice", title))
    story.append(Paragraph(
        "Compound math is straightforward. Hitting these targets consistently is not.",
        subtitle,
    ))

    story.append(Paragraph("What a daily target requires", sect))
    story.append(Paragraph(
        "Hitting 1–3% per day requires winning trades that net the target after losses. "
        "If your strategy has a 60% win rate and a 1.5R reward-to-risk ratio, you net "
        "positive expectancy of about 0.5R per trade. To make 2% per day with 1% risk per "
        "trade, you need ~4 net winning trades per day on a typical 5–8 trade pace.",
        body,
    ))
    story.append(Spacer(1, 6))

    story.append(Paragraph("Why drawdowns matter more than averages", sect))
    story.append(Paragraph(
        "A 20% drawdown requires a 25% gain to recover. A 40% drawdown requires a 67% gain. "
        "Daily compound math cuts both ways — losing 2% per day for 50 days reduces $10,000 "
        f"to about ${START_BALANCE * (1 - 0.02)**50:,.2f}. Position sizing and stop-loss "
        "discipline matter more than the daily target itself.",
        body,
    ))
    story.append(Spacer(1, 6))

    story.append(Paragraph("Realistic expectations", sect))
    story.append(Paragraph(
        "Most professional traders target <b>15–40% annualized</b> returns, not "
        "<b>2,600%+ in 100 days</b>. The compound table shows what's mathematically possible, "
        "not what's typically achievable. Treat these targets as aspirational ceilings — "
        "consistent execution at half these rates is exceptional.",
        body,
    ))
    story.append(Spacer(1, 14))

    # Disclaimer block
    disclaimer_data = [[
        Paragraph(
            "<b>Disclaimer:</b> This document is illustrative and educational only. "
            "It is not a guarantee, prediction, or solicitation. Past performance does not "
            "indicate future results. Trading involves substantial risk of loss. "
            "Consult a licensed advisor before making financial decisions. Trade Copilot "
            "is donation-supported software and does not provide investment advice.",
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
        "Trade Copilot · buymeacoffee.com/dbutler · educational use only",
        foot,
    ))

    doc.build(story)
    return OUT


if __name__ == "__main__":
    path = build_pdf()
    print(f"✅ wrote {path}  ({path.stat().st_size:,} bytes)")
