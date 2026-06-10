"""partner strategy onboarding: registry slug, params, invites + submissions

Adds the schema behind the partner self-serve upload flow:
  - bots.strategy_slug            (partner strategies dispatch by registry slug)
  - strategy_state.config_json    (partner-tunable params, read by the runner)
  - StrategyType 'partner'         (Postgres enum value; SQLite stores VARCHAR)
  - partner_invites               (single-use owner-issued upload links)
  - partner_submissions           (what a partner uploaded, pending approval)

All steps tolerate missing parent tables / pre-existing columns so the
lightweight bootstrap path (metadata create_all in tests) and the full
Alembic path both succeed.

Revision ID: 0009_partner_strategies
Revises: 0008_broker_statements
Create Date: 2026-06-10
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect


revision: str = "0009_partner_strategies"
down_revision: Union[str, Sequence[str], None] = "0008_broker_statements"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_table(name: str) -> bool:
    return name in inspect(op.get_bind()).get_table_names()


def _has_column(table: str, column: str) -> bool:
    if not _has_table(table):
        return False
    return any(c["name"] == column for c in inspect(op.get_bind()).get_columns(table))


def _add_partner_enum_value() -> None:
    """On Postgres, add 'partner' to the strategytype enum if it's native.

    SQLite stores the enum as a plain VARCHAR (no CHECK), so nothing to do.
    ALTER TYPE ... ADD VALUE cannot run inside a transaction block, so we
    commit first and run it on the raw connection in autocommit.
    """
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    # Only if the enum type actually exists as a native type.
    exists = bind.exec_driver_sql(
        "SELECT 1 FROM pg_type WHERE typname = 'strategytype'"
    ).first()
    if not exists:
        return
    with bind.begin():
        pass  # close any open transaction started by alembic
    bind.exec_driver_sql("COMMIT")
    bind.exec_driver_sql(
        "ALTER TYPE strategytype ADD VALUE IF NOT EXISTS 'partner'"
    )
    bind.exec_driver_sql("BEGIN")


def upgrade() -> None:
    # 1. bots.strategy_slug
    if _has_table("bots") and not _has_column("bots", "strategy_slug"):
        with op.batch_alter_table("bots") as batch_op:
            batch_op.add_column(
                sa.Column("strategy_slug", sa.String(length=64), nullable=True)
            )

    # 2. strategy_state.config_json
    if _has_table("strategy_state") and not _has_column(
        "strategy_state", "config_json"
    ):
        with op.batch_alter_table("strategy_state") as batch_op:
            batch_op.add_column(
                sa.Column("config_json", sa.Text(), nullable=True)
            )

    # 3. enum value (Postgres only)
    _add_partner_enum_value()

    # 4. partner_invites
    if not _has_table("partner_invites"):
        op.create_table(
            "partner_invites",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("token", sa.String(length=64), nullable=False),
            sa.Column("label", sa.String(length=120), server_default=""),
            sa.Column("partner_name_hint", sa.String(length=120), nullable=True),
            sa.Column("partner_email_hint", sa.String(length=255), nullable=True),
            sa.Column(
                "created_by_user_id",
                sa.Integer(),
                sa.ForeignKey("users.id"),
                nullable=False,
            ),
            sa.Column(
                "trading_account_id",
                sa.Integer(),
                sa.ForeignKey("trading_accounts.id"),
                nullable=True,
            ),
            sa.Column(
                "auto_start",
                sa.Boolean(),
                server_default=sa.false(),
                nullable=False,
            ),
            sa.Column("created_at", sa.DateTime(), nullable=True),
            sa.Column("expires_at", sa.DateTime(), nullable=True),
            sa.Column("used_at", sa.DateTime(), nullable=True),
            sa.Column("revoked_at", sa.DateTime(), nullable=True),
            sa.UniqueConstraint("token", name="uq_partner_invite_token"),
        )
        op.create_index(
            "ix_partner_invites_created_by", "partner_invites", ["created_by_user_id"]
        )

    # 5. partner_submissions
    if not _has_table("partner_submissions"):
        op.create_table(
            "partner_submissions",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column(
                "invite_id",
                sa.Integer(),
                sa.ForeignKey("partner_invites.id"),
                nullable=False,
            ),
            sa.Column("partner_name", sa.String(length=120), nullable=False),
            sa.Column("partner_email", sa.String(length=255), nullable=False),
            sa.Column("strategy_name", sa.String(length=120), nullable=False),
            sa.Column("strategy_slug", sa.String(length=64), nullable=False),
            sa.Column(
                "instruments_csv", sa.String(length=512), server_default="NAS100"
            ),
            sa.Column("timeframe", sa.String(length=8), server_default="1m"),
            sa.Column("params_json", sa.Text(), nullable=True),
            sa.Column("backtest_notes", sa.Text(), nullable=True),
            sa.Column("delivery_type", sa.String(length=8), nullable=False),
            sa.Column("source_code", sa.Text(), nullable=True),
            sa.Column("source_filename", sa.String(length=255), nullable=True),
            sa.Column("endpoint_url", sa.String(length=512), nullable=True),
            sa.Column("endpoint_secret", sa.Text(), nullable=True),
            sa.Column("ast_scan_json", sa.Text(), nullable=True),
            sa.Column(
                "status",
                sa.String(length=16),
                server_default="pending",
                nullable=False,
            ),
            sa.Column("rejection_reason", sa.Text(), nullable=True),
            sa.Column(
                "reviewed_by_user_id",
                sa.Integer(),
                sa.ForeignKey("users.id"),
                nullable=True,
            ),
            sa.Column("reviewed_at", sa.DateTime(), nullable=True),
            sa.Column(
                "approved_bot_id",
                sa.Integer(),
                sa.ForeignKey("bots.id"),
                nullable=True,
            ),
            sa.Column("created_at", sa.DateTime(), nullable=True),
            sa.Column("updated_at", sa.DateTime(), nullable=True),
        )
        op.create_index(
            "ix_partner_submissions_invite_id",
            "partner_submissions",
            ["invite_id"],
        )
        op.create_index(
            "ix_partner_submissions_partner_email",
            "partner_submissions",
            ["partner_email"],
        )
        op.create_index(
            "ix_partner_submissions_strategy_slug",
            "partner_submissions",
            ["strategy_slug"],
        )
        op.create_index(
            "ix_partner_submissions_status",
            "partner_submissions",
            ["status"],
        )


def downgrade() -> None:
    if _has_table("partner_submissions"):
        op.drop_table("partner_submissions")
    if _has_table("partner_invites"):
        op.drop_table("partner_invites")
    if _has_column("strategy_state", "config_json"):
        with op.batch_alter_table("strategy_state") as batch_op:
            batch_op.drop_column("config_json")
    if _has_column("bots", "strategy_slug"):
        with op.batch_alter_table("bots") as batch_op:
            batch_op.drop_column("strategy_slug")
    # Postgres enum value additions are not reverted (ALTER TYPE ... DROP VALUE
    # is unsupported); leaving 'partner' in the enum is harmless.
