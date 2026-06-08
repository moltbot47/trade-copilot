"""strategy_accounts — per-bot isolation demo accounts + outcome attribution.

Adds the ``strategy_accounts`` table (the isolation-harness primitive) and a
nullable ``trade_outcomes.strategy_account_id`` FK so the isolation equity
curve filters cleanly without mixing in multi-user copy-runner trades.

Both up and down tolerate the parent tables not existing (pure-Alembic-from
-empty test path) — same bootstrap rationale as 0002/0003.

Revision ID: 0004_strategy_accounts
Revises: 0003_user_risk_appetite
Create Date: 2026-05-16
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect


revision: str = "0004_strategy_accounts"
down_revision: Union[str, Sequence[str], None] = "0003_user_risk_appetite"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_table(name: str) -> bool:
    return name in inspect(op.get_bind()).get_table_names()


def _has_column(table: str, column: str) -> bool:
    return any(c["name"] == column for c in inspect(op.get_bind()).get_columns(table))


def upgrade() -> None:
    if not _has_table("strategy_accounts"):
        op.create_table(
            "strategy_accounts",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column(
                "bot_id",
                sa.Integer(),
                sa.ForeignKey("bots.id"),
                nullable=False,
                index=True,
            ),
            sa.Column(
                "user_id",
                sa.Integer(),
                sa.ForeignKey("users.id"),
                nullable=False,
                index=True,
            ),
            sa.Column("label", sa.String(length=120), server_default=""),
            sa.Column(
                "tradelocker_account_id", sa.String(length=64), nullable=False
            ),
            sa.Column(
                "tradelocker_acc_num",
                sa.String(length=16),
                server_default="1",
            ),
            sa.Column(
                "tradelocker_env", sa.String(length=8), server_default="demo"
            ),
            sa.Column("timeframe", sa.String(length=8), server_default="1m"),
            sa.Column(
                "start_offset_seconds",
                sa.Integer(),
                server_default="0",
            ),
            sa.Column(
                "is_active", sa.Boolean(), server_default=sa.true()
            ),
            sa.Column("created_at", sa.DateTime(), nullable=True),
            sa.UniqueConstraint("bot_id", name="uq_strategy_account_bot"),
            sa.UniqueConstraint(
                "tradelocker_account_id", name="uq_strategy_account_tlacct"
            ),
        )

    if _has_table("trade_outcomes") and not _has_column(
        "trade_outcomes", "strategy_account_id"
    ):
        op.add_column(
            "trade_outcomes",
            sa.Column(
                "strategy_account_id",
                sa.Integer(),
                sa.ForeignKey("strategy_accounts.id"),
                nullable=True,
                index=True,
            ),
        )


def downgrade() -> None:
    if _has_table("trade_outcomes") and _has_column(
        "trade_outcomes", "strategy_account_id"
    ):
        op.drop_column("trade_outcomes", "strategy_account_id")
    if _has_table("strategy_accounts"):
        op.drop_table("strategy_accounts")
