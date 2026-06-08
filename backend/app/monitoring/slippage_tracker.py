"""Slippage tracker — per-trade audit logger.

Writes to ``slippage_records`` at three lifecycle points so the 2-week
partner audit can compare backtest expectation against live broker fills
objectively. The same three-phase pattern feeds the partner dashboard's
real-time signal/fill/close stream and the daily Discord summary.

Lifecycle:

  1. ``record_entry_signal(...)`` — strategy emitted a signal. Persists
     the expected_entry_price, hard/trailing stop distances, and
     bar_close_ts. Status = "pending". Returns the new record id.

  2. ``record_fill(record_id, ...)`` — broker confirmed the order fill.
     Updates with actual_entry_price, fill_ts, entry_slippage_pts,
     and broker_fill_response_json. Status = "open". Latency from
     bar_close_ts to fill is computed here.

  3. ``finalize_record(record_id, ...)`` — position closed. Updates with
     exit_type, peak_price, actual_exit_price, exit_slippage_pts, the
     strategy-vs-real P&L pair, and broker_close_response_json.
     Status = "closed".

Read helpers for the partner dashboard and EOD summary:

  - ``list_records_for_user(...)``: time-range query scoped to a user
    (and optional strategy/account filters).
  - ``compute_daily_summary(...)``: aggregate stats for the EOD report:
    trade count, expected vs real P&L, average + worst slippage, latency
    percentiles, broker-vs-bot reconciliation flag.

All write functions handle their own DB session so callers can fire-and-
forget. The runner / signal_router / position_monitor only need to know
which lifecycle hook to call and what fields to pass.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from typing import Any, Optional

from sqlalchemy import and_, func, select
from sqlalchemy.orm import Session

from app.db.database import SessionLocal
from app.db.models import SlippageRecord

logger = logging.getLogger(__name__)


# ----------------------------------------------------------------------- #
# write helpers — called from runner / signal_router / position_monitor
# ----------------------------------------------------------------------- #
def record_entry_signal(
    *,
    user_id: int,
    strategy_name: str,
    account_id: str,
    symbol: str,
    side: str,
    bar_close_ts: datetime,
    bar_close_price: float,
    expected_entry_price: float,
    hard_stop_distance_pts: float = 0.0,
    trailing_stop_distance_pts: float = 0.0,
    early_stop_condition: str = "",
    execution_id: Optional[int] = None,
    extra: Optional[dict] = None,
    db: Optional[Session] = None,
) -> int:
    """Persist a new slippage_records row at signal-emit time.

    Returns the row id so the caller can update it when the fill confirms
    and again when the position closes. signal_ts is set to ``utcnow()``
    inside this function so callers don't have to thread it through.

    If ``db`` is provided we use it (caller owns the lifecycle); otherwise
    we open our own short-lived session.
    """
    now = datetime.utcnow()
    signal_latency_ms = max(
        0, int((now - bar_close_ts).total_seconds() * 1000)
    )

    own_session = db is None
    if own_session:
        db = SessionLocal()
    try:
        record = SlippageRecord(
            user_id=user_id,
            strategy_name=strategy_name,
            account_id=account_id,
            execution_id=execution_id,
            symbol=symbol,
            side=side,
            status="pending",
            bar_close_ts=bar_close_ts,
            signal_ts=now,
            bar_close_price=bar_close_price,
            expected_entry_price=expected_entry_price,
            hard_stop_distance_pts=hard_stop_distance_pts,
            trailing_stop_distance_pts=trailing_stop_distance_pts,
            early_stop_condition=early_stop_condition,
            signal_latency_ms=signal_latency_ms,
            extra_json=json.dumps(extra or {}),
        )
        db.add(record)
        db.commit()
        db.refresh(record)
        return record.id
    finally:
        if own_session:
            db.close()


def record_fill(
    record_id: int,
    *,
    actual_entry_price: float,
    order_submit_ts: Optional[datetime] = None,
    order_ack_ts: Optional[datetime] = None,
    fill_ts: Optional[datetime] = None,
    broker_response: Optional[dict] = None,
    execution_id: Optional[int] = None,
    db: Optional[Session] = None,
) -> None:
    """Update a pending record with broker fill data. Computes entry
    slippage and latency components.

    Slippage convention (signed):
      - For longs:  actual - expected      (positive = paid more, worse)
      - For shorts: expected - actual      (positive = sold for less, worse)

    A positive slippage is always "worse than expected" regardless of side
    so per-trade and aggregate numbers are directly interpretable.
    """
    own_session = db is None
    if own_session:
        db = SessionLocal()
    try:
        record = db.get(SlippageRecord, record_id)
        if record is None:
            logger.warning("record_fill: slippage_record id=%s not found", record_id)
            return

        ft = fill_ts or datetime.utcnow()
        record.fill_ts = ft
        if order_submit_ts is not None:
            record.order_submit_ts = order_submit_ts
        if order_ack_ts is not None:
            record.order_ack_ts = order_ack_ts
        record.actual_entry_price = float(actual_entry_price)

        if record.side.lower() == "buy":
            record.entry_slippage_pts = (
                float(actual_entry_price) - record.expected_entry_price
            )
        else:
            record.entry_slippage_pts = (
                record.expected_entry_price - float(actual_entry_price)
            )

        # Latency components — derive what we can. fill_latency uses
        # order_ack_ts when present so we attribute correctly between
        # network + broker phases.
        record.total_latency_ms = max(
            0, int((ft - record.bar_close_ts).total_seconds() * 1000)
        )
        if order_submit_ts is not None and record.signal_ts is not None:
            record.submit_latency_ms = max(
                0,
                int(
                    (order_submit_ts - record.signal_ts).total_seconds() * 1000
                ),
            )
        if order_ack_ts is not None and order_submit_ts is not None:
            record.broker_ack_latency_ms = max(
                0,
                int(
                    (order_ack_ts - order_submit_ts).total_seconds() * 1000
                ),
            )
        if order_ack_ts is not None:
            record.fill_latency_ms = max(
                0, int((ft - order_ack_ts).total_seconds() * 1000)
            )

        if broker_response is not None:
            record.broker_fill_response_json = json.dumps(broker_response)
        if execution_id is not None:
            record.execution_id = execution_id

        record.status = "open"
        db.commit()
    finally:
        if own_session:
            db.close()


def finalize_record(
    record_id: int,
    *,
    exit_type: str,
    actual_exit_price: float,
    expected_exit_price: Optional[float] = None,
    peak_price: Optional[float] = None,
    closed_ts: Optional[datetime] = None,
    broker_response: Optional[dict] = None,
    point_value_usd: float = 1.0,
    db: Optional[Session] = None,
) -> None:
    """Finalize a record on position close.

    Computes:
      - exit_slippage_pts:  signed, same "positive = worse" convention as entry
      - strategy_pnl_pts:   what the trade would have produced if fills hit expected
      - real_pnl_pts:       what actually happened
      - slippage_total_pts: strategy_pnl_pts - real_pnl_pts (edge erosion)
      - slippage_total_dollars: at point_value_usd × slippage_total_pts

    ``point_value_usd`` lets the caller pass the dollar value per point for
    the symbol+size combination so the dashboard can show friction in
    actual dollars without re-deriving it.
    """
    own_session = db is None
    if own_session:
        db = SessionLocal()
    try:
        record = db.get(SlippageRecord, record_id)
        if record is None:
            logger.warning("finalize_record: id=%s not found", record_id)
            return

        ct = closed_ts or datetime.utcnow()
        record.closed_ts = ct
        record.exit_type = exit_type
        record.actual_exit_price = float(actual_exit_price)
        if peak_price is not None:
            record.peak_price = float(peak_price)
        if expected_exit_price is not None:
            record.expected_exit_price = float(expected_exit_price)

        # Exit slippage — positive = worse than expected
        if record.expected_exit_price is not None:
            if record.side.lower() == "buy":
                # Long: we want a HIGH exit. Lower-than-expected is worse.
                record.exit_slippage_pts = (
                    record.expected_exit_price - float(actual_exit_price)
                )
            else:
                # Short: we want a LOW exit. Higher-than-expected is worse.
                record.exit_slippage_pts = (
                    float(actual_exit_price) - record.expected_exit_price
                )

        # P&L pair — strategy vs real, both in points. Signed by direction.
        if record.side.lower() == "buy":
            real_pnl = float(actual_exit_price) - (
                record.actual_entry_price or record.expected_entry_price
            )
            if record.expected_exit_price is not None:
                strategy_pnl = (
                    record.expected_exit_price - record.expected_entry_price
                )
            else:
                strategy_pnl = None
        else:
            real_pnl = (
                record.actual_entry_price or record.expected_entry_price
            ) - float(actual_exit_price)
            if record.expected_exit_price is not None:
                strategy_pnl = (
                    record.expected_entry_price - record.expected_exit_price
                )
            else:
                strategy_pnl = None

        record.real_pnl_pts = real_pnl
        if strategy_pnl is not None:
            record.strategy_pnl_pts = strategy_pnl
            record.slippage_total_pts = strategy_pnl - real_pnl
            record.slippage_total_dollars = (
                record.slippage_total_pts * point_value_usd
            )

        if broker_response is not None:
            record.broker_close_response_json = json.dumps(broker_response)

        record.status = "closed"
        db.commit()
    finally:
        if own_session:
            db.close()


def mark_rejected(record_id: int, *, db: Optional[Session] = None) -> None:
    """Mark a pending signal as rejected (broker said no, or runner gave up).

    Used so the partner dashboard's "today's signals" count matches what
    the strategy actually emitted vs what fell through to the broker.
    """
    own_session = db is None
    if own_session:
        db = SessionLocal()
    try:
        record = db.get(SlippageRecord, record_id)
        if record is None:
            return
        record.status = "rejected"
        db.commit()
    finally:
        if own_session:
            db.close()


# ----------------------------------------------------------------------- #
# read helpers — used by the partner dashboard and EOD summary
# ----------------------------------------------------------------------- #
def list_records_for_user(
    user_id: int,
    *,
    since: Optional[datetime] = None,
    until: Optional[datetime] = None,
    strategy_name: Optional[str] = None,
    account_id: Optional[str] = None,
    status: Optional[str] = None,
    limit: int = 500,
    db: Optional[Session] = None,
) -> list[SlippageRecord]:
    """Time-range query scoped to a user, with optional filters.

    Default window is the trailing 24h if no ``since`` is given. The
    composite index (user_id, strategy_name, created_at) keeps this off
    a full scan even when one user runs many strategies.
    """
    if since is None:
        since = datetime.utcnow() - timedelta(days=1)
    if until is None:
        until = datetime.utcnow()

    own_session = db is None
    if own_session:
        db = SessionLocal()
    try:
        stmt = (
            select(SlippageRecord)
            .where(
                and_(
                    SlippageRecord.user_id == user_id,
                    SlippageRecord.created_at >= since,
                    SlippageRecord.created_at <= until,
                )
            )
            .order_by(SlippageRecord.created_at.desc())
            .limit(limit)
        )
        if strategy_name:
            stmt = stmt.where(SlippageRecord.strategy_name == strategy_name)
        if account_id:
            stmt = stmt.where(SlippageRecord.account_id == account_id)
        if status:
            stmt = stmt.where(SlippageRecord.status == status)
        return list(db.scalars(stmt).all())
    finally:
        if own_session:
            db.close()


def compute_daily_summary(
    user_id: int,
    *,
    day: Optional[datetime] = None,
    strategy_name: Optional[str] = None,
    account_id: Optional[str] = None,
    db: Optional[Session] = None,
) -> dict[str, Any]:
    """Aggregate stats for a single UTC day, scoped to one user (and
    optionally one strategy).

    Powers the Discord EOD summary AND the partner dashboard's "today"
    summary card. Returns:

      {
        "user_id": int,
        "day": "2026-06-08",
        "strategy_name": str | None,
        "signals_emitted": int,       # all rows created
        "signals_rejected": int,      # status="rejected" count
        "trades_closed": int,         # status="closed" count
        "trades_open": int,           # status="open" count

        "strategy_pnl_pts": float,    # sum across closed trades
        "real_pnl_pts": float,
        "edge_erosion_pts": float,    # strategy - real (positive = friction ate the edge)
        "edge_erosion_dollars": float,

        "avg_entry_slippage_pts": float,
        "worst_entry_slippage_pts": float,
        "avg_exit_slippage_pts": float | None,
        "worst_exit_slippage_pts": float | None,

        "avg_total_latency_ms": int,
        "p95_total_latency_ms": int,
        "worst_total_latency_ms": int,
      }
    """
    if day is None:
        day = datetime.utcnow()
    start = datetime(day.year, day.month, day.day)
    end = start + timedelta(days=1)

    own_session = db is None
    if own_session:
        db = SessionLocal()
    try:
        base_filter = and_(
            SlippageRecord.user_id == user_id,
            SlippageRecord.created_at >= start,
            SlippageRecord.created_at < end,
        )
        if strategy_name:
            base_filter = and_(base_filter, SlippageRecord.strategy_name == strategy_name)
        if account_id:
            base_filter = and_(base_filter, SlippageRecord.account_id == account_id)

        records = list(db.scalars(select(SlippageRecord).where(base_filter)).all())
        closed = [r for r in records if r.status == "closed"]
        rejected = [r for r in records if r.status == "rejected"]
        open_ = [r for r in records if r.status == "open"]

        def _nonnull(values: list[Optional[float]]) -> list[float]:
            return [v for v in values if v is not None]

        def _pct(values: list[float], q: float) -> float:
            if not values:
                return 0.0
            vs = sorted(values)
            k = max(0, min(len(vs) - 1, int(round(q * (len(vs) - 1)))))
            return vs[k]

        entry_slippages = _nonnull([r.entry_slippage_pts for r in records])
        exit_slippages = _nonnull([r.exit_slippage_pts for r in closed])
        total_latencies = _nonnull([r.total_latency_ms for r in records])

        strategy_pnl_total = sum(r.strategy_pnl_pts or 0.0 for r in closed)
        real_pnl_total = sum(r.real_pnl_pts or 0.0 for r in closed)
        edge_erosion_pts = strategy_pnl_total - real_pnl_total
        edge_erosion_dollars = sum(r.slippage_total_dollars or 0.0 for r in closed)

        return {
            "user_id": user_id,
            "day": start.date().isoformat(),
            "strategy_name": strategy_name,
            "signals_emitted": len(records),
            "signals_rejected": len(rejected),
            "trades_closed": len(closed),
            "trades_open": len(open_),
            "strategy_pnl_pts": round(strategy_pnl_total, 4),
            "real_pnl_pts": round(real_pnl_total, 4),
            "edge_erosion_pts": round(edge_erosion_pts, 4),
            "edge_erosion_dollars": round(edge_erosion_dollars, 4),
            "avg_entry_slippage_pts": (
                round(sum(entry_slippages) / len(entry_slippages), 4)
                if entry_slippages
                else 0.0
            ),
            "worst_entry_slippage_pts": (
                round(max(entry_slippages), 4) if entry_slippages else 0.0
            ),
            "avg_exit_slippage_pts": (
                round(sum(exit_slippages) / len(exit_slippages), 4)
                if exit_slippages
                else None
            ),
            "worst_exit_slippage_pts": (
                round(max(exit_slippages), 4) if exit_slippages else None
            ),
            "avg_total_latency_ms": (
                int(sum(total_latencies) / len(total_latencies))
                if total_latencies
                else 0
            ),
            "p95_total_latency_ms": int(_pct(total_latencies, 0.95)),
            "worst_total_latency_ms": int(max(total_latencies)) if total_latencies else 0,
        }
    finally:
        if own_session:
            db.close()


def count_pending_since(
    user_id: int, since: datetime, *, db: Optional[Session] = None
) -> int:
    """How many pending signals are older than ``since`` — flag candidates
    for the reconciliation pipeline (signal emitted but no broker fill
    confirmation came back). Used by health alerts."""
    own_session = db is None
    if own_session:
        db = SessionLocal()
    try:
        return int(
            db.scalar(
                select(func.count())
                .select_from(SlippageRecord)
                .where(
                    SlippageRecord.user_id == user_id,
                    SlippageRecord.status == "pending",
                    SlippageRecord.signal_ts < since,
                )
            )
            or 0
        )
    finally:
        if own_session:
            db.close()
