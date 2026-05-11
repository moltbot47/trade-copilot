"""subscription.allowed_instruments — per-user instrument filter.

Adds a nullable CSV column to ``subscriptions``. NULL means "inherit all
of the parent bot's instruments" — preserving the prior all-or-nothing
behavior for every existing row. New subscribers can pass a subset.

Schema-creation note: this codebase has a split-brain bootstrap. The
production lifespan runs ``Base.metadata.create_all`` and then stamps
Alembic at head; pure-Alembic upgrades from an empty database (as used
by the test suite) do not create the table here at all. So both
``upgrade`` and ``downgrade`` are no-ops when the ``subscriptions``
table is absent — the column will already exist via ``create_all`` by
the time real data lands.

Revision ID: 0002_subscription_allowed_instruments
Revises: 0001_baseline
Create Date: 2026-05-11
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect


revision: str = "0002_subscription_allowed_instruments"
down_revision: Union[str, Sequence[str], None] = "0001_baseline"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_table(name: str) -> bool:
    return name in inspect(op.get_bind()).get_table_names()


def _has_column(table: str, column: str) -> bool:
    return any(c["name"] == column for c in inspect(op.get_bind()).get_columns(table))


def upgrade() -> None:
    if not _has_table("subscriptions"):
        return  # pure-Alembic-from-empty path; create_all hasn't run yet
    if _has_column("subscriptions", "allowed_instruments"):
        return  # idempotent for environments that already shipped this column
    op.add_column(
        "subscriptions",
        sa.Column("allowed_instruments", sa.String(length=512), nullable=True),
    )


def downgrade() -> None:
    if not _has_table("subscriptions"):
        return
    if not _has_column("subscriptions", "allowed_instruments"):
        return
    op.drop_column("subscriptions", "allowed_instruments")
