"""user.risk_appetite — drives tiny-account advisor recommendations.

Adds a string column with default 'balanced'. Existing rows backfill to
'balanced' so the advisor's defaults work for users who haven't picked.

Like the 0002 migration, both up and down are tolerant of the table not
existing (pure-Alembic-from-empty test path) — see that file's docstring
for the bootstrap rationale.

Revision ID: 0003_user_risk_appetite
Revises: 0002_subscription_allowed_instruments
Create Date: 2026-05-11
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect


revision: str = "0003_user_risk_appetite"
down_revision: Union[str, Sequence[str], None] = "0002_subscription_allowed_instruments"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_table(name: str) -> bool:
    return name in inspect(op.get_bind()).get_table_names()


def _has_column(table: str, column: str) -> bool:
    return any(c["name"] == column for c in inspect(op.get_bind()).get_columns(table))


def upgrade() -> None:
    if not _has_table("users"):
        return
    if _has_column("users", "risk_appetite"):
        return
    op.add_column(
        "users",
        sa.Column(
            "risk_appetite",
            sa.String(length=16),
            nullable=False,
            server_default="balanced",
        ),
    )


def downgrade() -> None:
    if not _has_table("users"):
        return
    if not _has_column("users", "risk_appetite"):
        return
    op.drop_column("users", "risk_appetite")
