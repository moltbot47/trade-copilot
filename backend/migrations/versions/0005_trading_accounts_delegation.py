"""trading_accounts + account_access_grants + audit_log.account_id

Phase A of the DOM employee-delegation model. Adds:
  - trading_accounts          (broker accounts an owner can delegate)
  - account_access_grants     (grantee_user_id may act on account_id with role)
  - audit_log.account_id      (nullable FK so DOM events record which account)

Both up and down tolerate missing parent tables (same bootstrap rationale as
prior migrations).

Revision ID: 0005_trading_accounts_delegation
Revises: 0004_strategy_accounts
Create Date: 2026-06-02
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect


revision: str = "0005_trading_accounts_delegation"
down_revision: Union[str, Sequence[str], None] = "0004_strategy_accounts"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_table(name: str) -> bool:
    return name in inspect(op.get_bind()).get_table_names()


def _has_column(table: str, column: str) -> bool:
    return any(c["name"] == column for c in inspect(op.get_bind()).get_columns(table))


def upgrade() -> None:
    if not _has_table("trading_accounts"):
        op.create_table(
            "trading_accounts",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column(
                "owner_user_id",
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
                "tradelocker_acc_num", sa.String(length=16), server_default="1"
            ),
            sa.Column(
                "tradelocker_env", sa.String(length=8), server_default="demo"
            ),
            sa.Column("is_active", sa.Boolean(), server_default=sa.true()),
            sa.Column("created_at", sa.DateTime(), nullable=True),
            sa.UniqueConstraint(
                "owner_user_id",
                "tradelocker_account_id",
                name="uq_tradingaccount_owner_tlacct",
            ),
        )

    if not _has_table("account_access_grants"):
        op.create_table(
            "account_access_grants",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column(
                "account_id",
                sa.Integer(),
                sa.ForeignKey("trading_accounts.id"),
                nullable=False,
                index=True,
            ),
            sa.Column(
                "grantee_user_id",
                sa.Integer(),
                sa.ForeignKey("users.id"),
                nullable=False,
                index=True,
            ),
            sa.Column(
                "role",
                sa.String(length=16),
                server_default="trader",
                nullable=False,
            ),
            sa.Column(
                "granted_by_user_id",
                sa.Integer(),
                sa.ForeignKey("users.id"),
                nullable=False,
            ),
            sa.Column("created_at", sa.DateTime(), nullable=True),
            sa.Column("expires_at", sa.DateTime(), nullable=True),
            sa.Column("revoked_at", sa.DateTime(), nullable=True),
            sa.Column("max_lot_per_order", sa.Float(), nullable=True),
            sa.Column("max_daily_loss_usd", sa.Float(), nullable=True),
            sa.Column(
                "allowed_instruments_csv",
                sa.String(length=512),
                nullable=True,
            ),
        )

    if _has_table("audit_log") and not _has_column("audit_log", "account_id"):
        # SQLite cannot ALTER TABLE to add a FK in place — batch mode does
        # a copy-and-move under the hood. Postgres takes the same path and
        # just emits an ADD COLUMN ... REFERENCES.
        with op.batch_alter_table("audit_log") as batch_op:
            batch_op.add_column(
                sa.Column(
                    "account_id",
                    sa.Integer(),
                    nullable=True,
                )
            )
            batch_op.create_foreign_key(
                "fk_auditlog_account",
                "trading_accounts",
                ["account_id"],
                ["id"],
            )
            batch_op.create_index(
                "ix_audit_log_account_id", ["account_id"]
            )


def downgrade() -> None:
    if _has_table("audit_log") and _has_column("audit_log", "account_id"):
        with op.batch_alter_table("audit_log") as batch_op:
            batch_op.drop_index("ix_audit_log_account_id")
            batch_op.drop_constraint("fk_auditlog_account", type_="foreignkey")
            batch_op.drop_column("account_id")
    if _has_table("account_access_grants"):
        op.drop_table("account_access_grants")
    if _has_table("trading_accounts"):
        op.drop_table("trading_accounts")
