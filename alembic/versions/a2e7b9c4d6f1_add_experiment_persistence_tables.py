"""Add durable support-experiment persistence tables.

Revision ID: a2e7b9c4d6f1
Revises: d8f4e2a1b9c7
Create Date: 2026-09-02
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "a2e7b9c4d6f1"
down_revision: str | Sequence[str] | None = "d8f4e2a1b9c7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "experiments",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("contract", postgresql.JSONB(), nullable=False),
        sa.Column("contract_hash", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("execution_id", sa.Uuid(), nullable=True),
        sa.Column("error_code", sa.String(length=100), nullable=True),
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
    op.create_index("ix_experiments_status", "experiments", ["status"], unique=False)
    op.create_index("ix_experiments_execution_id", "experiments", ["execution_id"], unique=False)

    op.create_table(
        "experiment_events",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("experiment_id", sa.Uuid(), nullable=False),
        sa.Column("execution_id", sa.Uuid(), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(length=60), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column("emitted_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["experiment_id"], ["experiments.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "experiment_id",
            "execution_id",
            "sequence",
            name="uq_experiment_events_execution_sequence",
        ),
    )
    op.create_index(
        "ix_experiment_events_execution_sequence",
        "experiment_events",
        ["experiment_id", "execution_id", "sequence"],
        unique=False,
    )

    op.create_table(
        "experiment_iteration_outcomes",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("experiment_id", sa.Uuid(), nullable=False),
        sa.Column("execution_id", sa.Uuid(), nullable=False),
        sa.Column("iteration_id", sa.Uuid(), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("variant", sa.String(length=20), nullable=False),
        sa.Column("repetition", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("error_code", sa.String(length=100), nullable=True),
        sa.Column("evidence", postgresql.JSONB(), nullable=True),
        sa.Column("evidence_hash", sa.String(length=64), nullable=True),
        sa.Column(
            "recorded_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["experiment_id"], ["experiments.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "experiment_id",
            "execution_id",
            "iteration_id",
            name="uq_experiment_iteration_outcomes_execution_iteration",
        ),
        sa.CheckConstraint(
            "(status = 'completed' AND error_code IS NULL "
            "AND evidence IS NOT NULL AND evidence_hash IS NOT NULL) "
            "OR (status = 'failed' AND error_code IS NOT NULL "
            "AND evidence IS NULL AND evidence_hash IS NULL)",
            name="ck_experiment_iteration_outcomes_terminal_evidence",
        ),
    )
    op.create_index(
        "ix_experiment_iteration_outcomes_execution_ordinal",
        "experiment_iteration_outcomes",
        ["experiment_id", "execution_id", "ordinal"],
        unique=False,
    )

    op.create_table(
        "experiment_results",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("experiment_id", sa.Uuid(), nullable=False),
        sa.Column("execution_id", sa.Uuid(), nullable=False),
        sa.Column("contract_hash", sa.String(length=64), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("result", postgresql.JSONB(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["experiment_id"], ["experiments.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("experiment_id", name="uq_experiment_results_experiment"),
    )
    op.execute(
        """
        CREATE FUNCTION reject_experiment_iteration_outcome_update()
        RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'experiment iteration outcomes are immutable';
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        """
        CREATE TRIGGER prevent_experiment_iteration_outcome_update
        BEFORE UPDATE ON experiment_iteration_outcomes
        FOR EACH ROW EXECUTE FUNCTION reject_experiment_iteration_outcome_update()
        """
    )
    op.execute(
        """
        CREATE FUNCTION reject_experiment_result_update()
        RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'experiment results are immutable';
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        """
        CREATE TRIGGER prevent_experiment_result_update
        BEFORE UPDATE ON experiment_results
        FOR EACH ROW EXECUTE FUNCTION reject_experiment_result_update()
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER prevent_experiment_result_update ON experiment_results")
    op.execute("DROP FUNCTION reject_experiment_result_update()")
    op.drop_table("experiment_results")
    op.execute(
        "DROP TRIGGER prevent_experiment_iteration_outcome_update "
        "ON experiment_iteration_outcomes"
    )
    op.execute("DROP FUNCTION reject_experiment_iteration_outcome_update()")
    op.drop_index(
        "ix_experiment_iteration_outcomes_execution_ordinal",
        table_name="experiment_iteration_outcomes",
    )
    op.drop_table("experiment_iteration_outcomes")
    op.drop_index("ix_experiment_events_execution_sequence", table_name="experiment_events")
    op.drop_table("experiment_events")
    op.drop_index("ix_experiments_execution_id", table_name="experiments")
    op.drop_index("ix_experiments_status", table_name="experiments")
    op.drop_table("experiments")
