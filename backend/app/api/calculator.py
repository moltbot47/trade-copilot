"""Public PDF calculator endpoint — no auth (lead-gen tool)."""
from __future__ import annotations

import logging
from datetime import datetime

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.core.compound_pdf import generate_compound_pdf
from app.core.rate_limit import limiter

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/calculator", tags=["calculator"])


class CalculatorInput(BaseModel):
    start_balance: float = Field(default=10_000.0, gt=0, le=10_000_000)
    daily_rate_pct: float = Field(default=2.0, gt=0, le=10)
    days: int = Field(default=100, ge=7, le=365)
    requester_label: str | None = Field(default=None, max_length=120)


@router.post("/generate")
@limiter.limit("30/minute")
def generate(request: Request, payload: CalculatorInput) -> StreamingResponse:
    """Returns a personalized compound-growth PDF as a download."""
    try:
        pdf_bytes = generate_compound_pdf(
            start_balance=payload.start_balance,
            daily_rate_pct=payload.daily_rate_pct,
            days=payload.days,
            requester_label=payload.requester_label,
        )
    except Exception as exc:
        logger.exception("compound pdf generation failed")
        raise HTTPException(status_code=500, detail=f"pdf generation failed: {exc}")

    fname = (
        f"trade-copilot_compound_"
        f"{int(payload.start_balance)}_"
        f"{payload.daily_rate_pct:.1f}pct_"
        f"{payload.days}d_"
        f"{datetime.utcnow():%Y%m%d}.pdf"
    )
    headers = {"Content-Disposition": f'attachment; filename="{fname}"'}
    return StreamingResponse(
        iter([pdf_bytes]),
        media_type="application/pdf",
        headers=headers,
    )


@router.post("/preview")
def preview(payload: CalculatorInput) -> dict:
    """Lightweight live preview — no PDF, just numbers for the live form."""
    rate = payload.daily_rate_pct / 100.0
    bal = payload.start_balance
    series_end = bal * (1 + rate) ** payload.days
    milestones = {}
    for d in (max(1, payload.days // 10), max(1, payload.days // 4),
              max(1, payload.days // 2), max(1, payload.days * 3 // 4), payload.days):
        milestones[d] = round(bal * (1 + rate) ** d, 2)
    return {
        "start_balance": payload.start_balance,
        "daily_rate_pct": payload.daily_rate_pct,
        "days": payload.days,
        "end_balance": round(series_end, 2),
        "multiplier": round(series_end / bal, 2),
        "milestones": milestones,
    }
