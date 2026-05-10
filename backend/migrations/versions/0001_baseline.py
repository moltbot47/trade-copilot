"""baseline — captures pre-Alembic schema state (no-op).

The current schema was created via SQLAlchemy ``Base.metadata.create_all``
plus the ``_apply_lightweight_migrations`` shim in ``app.main``. This
revision is intentionally empty so existing databases can be stamped
without rewriting tables; new migrations build on this baseline.

Revision ID: 0001_baseline
Revises:
Create Date: 2026-05-10
"""
from __future__ import annotations

from typing import Sequence, Union


revision: str = "0001_baseline"
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """No-op: existing DBs already match the current model definitions."""
    pass


def downgrade() -> None:
    """No-op: there is nothing to roll back."""
    pass
