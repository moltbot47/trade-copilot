"""broker_statements — periodic snapshots of broker truth per account.

The trust anchor for the partner audit. Each row captures the canonical
state of a TradingAccount at a point in time as the broker reported it:
balance, open positions, recent trades. Stored verbatim (raw JSON) plus
derived counts/totals so the dashboard can show drift trends without
re-parsing the snapshot every render.

Snapshots are immutable. The reconciliation diff endpoint compares the
slippage_records we wrote against the broker's reality and flags any
divergence — missing close response, P&L delta, ghost positions.

Both up and down tolerate missing parent tables for the bootstrap path.

Revision ID: 0008_broker_statements
Revises: 0007_partner_webhooks
Create Date: 2026-06-08
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect


revision: str = "0008_broker_statements"
down_revision: Union[str, Sequence[str], None] = "0007_partner_webhooks"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_table(name: str) -> bool:
    return name in inspect(op.get_bind()).get_table_names()


def upgrade() -> None:
    if _has_table("broker_statements"):
        return

    users_exists = _has_table("users")
    trading_accounts_exists = _has_table("trading_accounts")

    op.create_table(
        "broker_statements",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "owner_user_id",
            sa.Integer(),
            sa.ForeignKey("users.id") if users_exists else None,
            nullable=False,
            index=True,
        ),
        sa.Column(
            "trading_account_id",
            sa.Integer(),
            sa.ForeignKey("trading_accounts.id") if trading_accounts_exists else None,
            nullable=True,
            index=True,
        ),
        # Broker-side IDs (preserved even if our TradingAccount row is later
        # deleted — the historical snapshot doesn't need a live FK).
        sa.Column("tradelocker_account_id", sa.String(64), nullable=False, index=True),
        sa.Column("tradelocker_acc_num", sa.String(16), nullable=False),
        sa.Column("tradelocker_env", sa.String(8), nullable=False),
        # Snapshot window
        sa.Column(
            "pulled_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
            index=True,
        ),
        # Derived totals — easy to query without unpacking the raw JSON
        sa.Column("balance", sa.Float(), nullable=True),
        sa.Column("equity", sa.Float(), nullable=True),
        sa.Column("open_pnl", sa.Float(), nullable=True),
        sa.Column("positions_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("orders_count", sa.Integer(), nullable=False, server_default="0"),
        # Raw broker responses — verbatim for partner-side recompute
        sa.Column("raw_account_state_json", sa.Text(), nullable=True),
        sa.Column("raw_positions_json", sa.Text(), nullable=True),
        sa.Column("raw_orders_json", sa.Text(), nullable=True),
        # Tamper-detection hash — sha256(state || positions || orders)
        sa.Column("content_sha256", sa.String(64), nullable=False),
    )

    # Composite index for the partner dashboard's "snapshots for account
    # over time" query — keep off a full scan as snapshots accumulate.
    op.create_index(
        "ix_broker_statements_account_pulled",
        "broker_statements",
        ["tradelocker_account_id", "pulled_at"],
    )


def downgrade() -> None:
    if not _has_table("broker_statements"):
        return
    try:
        op.drop_index(
            "ix_broker_statements_account_pulled", table_name="broker_statements"
        )
    except Exception:
        pass
    op.drop_table("broker_statements")
