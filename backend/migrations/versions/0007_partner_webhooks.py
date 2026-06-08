"""account_access_grants — add partner_webhook_url + partner_webhook_secret

Adds the two columns the partner webhook hook (Task #6) needs to tee every
slippage_record event back to a partner-controlled endpoint signed with
their HMAC secret. Per AccountAccessGrant the same table that scopes the
partner's read access also configures where their independent audit
mirror lands.

Both up and down tolerate the parent table not existing (matches the
bootstrap rationale of prior migrations).

Revision ID: 0007_partner_webhooks
Revises: 0006_slippage_records
Create Date: 2026-06-08
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect


revision: str = "0007_partner_webhooks"
down_revision: Union[str, Sequence[str], None] = "0006_slippage_records"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_table(name: str) -> bool:
    return name in inspect(op.get_bind()).get_table_names()


def _has_column(table: str, column: str) -> bool:
    return any(c["name"] == column for c in inspect(op.get_bind()).get_columns(table))


def upgrade() -> None:
    if not _has_table("account_access_grants"):
        return
    if not _has_column("account_access_grants", "partner_webhook_url"):
        op.add_column(
            "account_access_grants",
            sa.Column("partner_webhook_url", sa.Text(), nullable=True),
        )
    if not _has_column("account_access_grants", "partner_webhook_secret_encrypted"):
        op.add_column(
            "account_access_grants",
            sa.Column(
                "partner_webhook_secret_encrypted", sa.Text(), nullable=True
            ),
        )


def downgrade() -> None:
    if not _has_table("account_access_grants"):
        return
    if _has_column("account_access_grants", "partner_webhook_secret_encrypted"):
        try:
            op.drop_column(
                "account_access_grants", "partner_webhook_secret_encrypted"
            )
        except Exception:
            pass
    if _has_column("account_access_grants", "partner_webhook_url"):
        try:
            op.drop_column("account_access_grants", "partner_webhook_url")
        except Exception:
            pass
