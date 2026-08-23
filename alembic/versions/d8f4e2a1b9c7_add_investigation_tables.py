"""Add investigation and event streaming tables for Phase 8.

Revision ID: d8f4e2a1b9c7
Revises: c9a6f3b1d2e4
Create Date: 2026-08-23
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "d8f4e2a1b9c7"
down_revision: str | Sequence[str] | None = "9f7c2a1d5e4b"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "investigations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("trace_ref", postgresql.JSONB(), nullable=False),
        sa.Column("world_ref", postgresql.JSONB(), nullable=False),
        sa.Column("slice_ref", postgresql.JSONB(), nullable=False),
        sa.Column("investigator_ref", postgresql.JSONB(), nullable=False),
        sa.Column("tested_agent_ref", postgresql.JSONB(), nullable=True),
        sa.Column("task_brief", sa.Text(), nullable=False),
        sa.Column("bridge_token_hash", sa.String(length=64), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("last_heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_investigations_status",
        "investigations",
        ["status"],
        unique=False,
    )
    op.create_index(
        "ix_investigations_bridge_token_hash",
        "investigations",
        ["bridge_token_hash"],
        unique=False,
    )

    op.create_table(
        "investigation_events",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("investigation_id", sa.Uuid(), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("type", sa.String(length=50), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column("emitted_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["investigation_id"],
            ["investigations.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "investigation_id",
            "seq",
            name="uq_investigation_events_seq",
        ),
    )
    op.create_index(
        "ix_investigation_events_inv_seq",
        "investigation_events",
        ["investigation_id", "seq"],
        unique=False,
    )

    op.create_table(
        "investigation_messages",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("investigation_id", sa.Uuid(), nullable=False),
        sa.Column("sender", sa.String(length=20), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("mode", sa.String(length=20), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["investigation_id"],
            ["investigations.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_investigation_messages_inv",
        "investigation_messages",
        ["investigation_id", "id"],
        unique=False,
    )

    op.create_table(
        "investigation_summaries",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("investigation_id", sa.Uuid(), nullable=False),
        sa.Column("findings", sa.Text(), nullable=False),
        sa.Column("next_step", sa.Text(), nullable=False),
        sa.Column("evidence_refs", postgresql.JSONB(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["investigation_id"],
            ["investigations.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("investigation_id", name="uq_investigation_summary_inv"),
    )


def downgrade() -> None:
    op.drop_table("investigation_summaries")
    op.drop_index("ix_investigation_messages_inv", table_name="investigation_messages")
    op.drop_table("investigation_messages")
    op.drop_index("ix_investigation_events_inv_seq", table_name="investigation_events")
    op.drop_table("investigation_events")
    op.drop_index("ix_investigations_bridge_token_hash", table_name="investigations")
    op.drop_index("ix_investigations_status", table_name="investigations")
    op.drop_table("investigations")
