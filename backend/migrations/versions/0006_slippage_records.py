"""slippage_records — per-trade audit table for backtest-vs-live comparison

Adds the ``slippage_records`` table written by the slippage tracker at three
lifecycle points (signal → fill → close). Foundation for the partner audit
pipeline (2-week comparison of backtest assumption vs real broker fills) and
the partner dashboard's live slippage/latency feed.

Both up and down tolerate missing parent tables (same bootstrap rationale as
prior migrations).

Revision ID: 0006_slippage_records
Revises: 0005_trading_accounts_delegation
Create Date: 2026-06-08
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect


revision: str = "0006_slippage_records"
down_revision: Union[str, Sequence[str], None] = "0005_trading_accounts_delegation"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_table(name: str) -> bool:
    return name in inspect(op.get_bind()).get_table_names()


def upgrade() -> None:
    if _has_table("slippage_records"):
        return

    # Parent FKs are nullable-safe — we still attempt the constraints so
    # production gets referential integrity, but if a parent table is
    # missing in an empty bootstrap path the create proceeds without it.
    users_exists = _has_table("users")
    executions_exists = _has_table("executions")

    op.create_table(
        "slippage_records",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "user_id",
            sa.Integer(),
            sa.ForeignKey("users.id") if users_exists else None,
            nullable=False,
            index=True,
        ),
        sa.Column("strategy_name", sa.String(64), nullable=False, index=True),
        sa.Column("account_id", sa.String(64), nullable=False),
        sa.Column(
            "execution_id",
            sa.Integer(),
            sa.ForeignKey("executions.id") if executions_exists else None,
            nullable=True,
            index=True,
        ),
        sa.Column("symbol", sa.String(32), nullable=False),
        sa.Column("side", sa.String(8), nullable=False),
        sa.Column(
            "status", sa.String(16), nullable=False, server_default="pending", index=True
        ),
        # Timing
        sa.Column("bar_close_ts", sa.DateTime(), nullable=False),
        sa.Column("signal_ts", sa.DateTime(), nullable=False),
        sa.Column("order_submit_ts", sa.DateTime(), nullable=True),
        sa.Column("order_ack_ts", sa.DateTime(), nullable=True),
        sa.Column("fill_ts", sa.DateTime(), nullable=True),
        sa.Column("closed_ts", sa.DateTime(), nullable=True),
        # Entry
        sa.Column("bar_close_price", sa.Float(), nullable=False),
        sa.Column("expected_entry_price", sa.Float(), nullable=False),
        sa.Column("actual_entry_price", sa.Float(), nullable=True),
        sa.Column("entry_slippage_pts", sa.Float(), nullable=True),
        # Stop / exit expectations
        sa.Column(
            "hard_stop_distance_pts",
            sa.Float(),
            nullable=False,
            server_default="0.0",
        ),
        sa.Column(
            "trailing_stop_distance_pts",
            sa.Float(),
            nullable=False,
            server_default="0.0",
        ),
        sa.Column(
            "early_stop_condition",
            sa.String(64),
            nullable=False,
            server_default="",
        ),
        # Exit
        sa.Column("exit_type", sa.String(32), nullable=True),
        sa.Column("peak_price", sa.Float(), nullable=True),
        sa.Column("expected_exit_price", sa.Float(), nullable=True),
        sa.Column("actual_exit_price", sa.Float(), nullable=True),
        sa.Column("exit_slippage_pts", sa.Float(), nullable=True),
        # P&L
        sa.Column("strategy_pnl_pts", sa.Float(), nullable=True),
        sa.Column("real_pnl_pts", sa.Float(), nullable=True),
        sa.Column("slippage_total_pts", sa.Float(), nullable=True),
        sa.Column("slippage_total_dollars", sa.Float(), nullable=True),
        # Latency
        sa.Column("signal_latency_ms", sa.Integer(), nullable=True),
        sa.Column("submit_latency_ms", sa.Integer(), nullable=True),
        sa.Column("broker_ack_latency_ms", sa.Integer(), nullable=True),
        sa.Column("fill_latency_ms", sa.Integer(), nullable=True),
        sa.Column("total_latency_ms", sa.Integer(), nullable=True),
        # Raw broker JSON (trust anchor for partner dashboard)
        sa.Column("broker_fill_response_json", sa.Text(), nullable=True),
        sa.Column("broker_close_response_json", sa.Text(), nullable=True),
        # Metadata
        sa.Column("extra_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column(
            "created_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
            index=True,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
    )

    # Composite index for partner dashboard time-range queries scoped by
    # user + strategy. Keeps the common query path off a full scan even
    # when one user runs many strategies.
    op.create_index(
        "ix_slippage_user_strategy_created",
        "slippage_records",
        ["user_id", "strategy_name", "created_at"],
    )


def downgrade() -> None:
    if not _has_table("slippage_records"):
        return
    try:
        op.drop_index("ix_slippage_user_strategy_created", table_name="slippage_records")
    except Exception:
        pass
    op.drop_table("slippage_records")
