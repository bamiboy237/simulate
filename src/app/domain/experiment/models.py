"""SQLAlchemy ORM models for durable support-experiment persistence."""

from datetime import datetime, timezone
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


class ExperimentRecord(Base):
    """Stores an exact experiment contract, its execution attempt, and lifecycle."""

    __tablename__ = "experiments"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    contract: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    contract_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(30), nullable=False, index=True)
    execution_id: Mapped[UUID | None] = mapped_column(Uuid, nullable=True, index=True)
    error_code: Mapped[str | None] = mapped_column(String(100), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    events: Mapped[list["ExperimentEventRecord"]] = relationship(
        "ExperimentEventRecord",
        back_populates="experiment",
        cascade="all, delete-orphan",
        order_by="ExperimentEventRecord.sequence",
    )
    iteration_outcomes: Mapped[list["ExperimentIterationOutcomeRecord"]] = relationship(
        "ExperimentIterationOutcomeRecord",
        back_populates="experiment",
        cascade="all, delete-orphan",
        order_by="ExperimentIterationOutcomeRecord.ordinal",
    )
    result: Mapped["ExperimentResultRecord | None"] = relationship(
        "ExperimentResultRecord",
        back_populates="experiment",
        uselist=False,
        cascade="all, delete-orphan",
    )


class ExperimentEventRecord(Base):
    """Stores one sequence-numbered event from a specific execution attempt."""

    __tablename__ = "experiment_events"
    __table_args__ = (
        UniqueConstraint(
            "experiment_id",
            "execution_id",
            "sequence",
            name="uq_experiment_events_execution_sequence",
        ),
        Index(
            "ix_experiment_events_execution_sequence",
            "experiment_id",
            "execution_id",
            "sequence",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    experiment_id: Mapped[UUID] = mapped_column(
        Uuid,
        ForeignKey("experiments.id", ondelete="CASCADE"),
        nullable=False,
    )
    execution_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    kind: Mapped[str] = mapped_column(String(60), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    emitted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    experiment: Mapped["ExperimentRecord"] = relationship(
        "ExperimentRecord",
        back_populates="events",
    )


class ExperimentIterationOutcomeRecord(Base):
    """Stores the safe terminal outcome for one planned iteration."""

    __tablename__ = "experiment_iteration_outcomes"
    __table_args__ = (
        CheckConstraint(
            "(status = 'completed' AND error_code IS NULL "
            "AND evidence IS NOT NULL AND evidence_hash IS NOT NULL) "
            "OR (status = 'failed' AND error_code IS NOT NULL "
            "AND evidence IS NULL AND evidence_hash IS NULL)",
            name="ck_experiment_iteration_outcomes_terminal_evidence",
        ),
        UniqueConstraint(
            "experiment_id",
            "execution_id",
            "iteration_id",
            name="uq_experiment_iteration_outcomes_execution_iteration",
        ),
        Index(
            "ix_experiment_iteration_outcomes_execution_ordinal",
            "experiment_id",
            "execution_id",
            "ordinal",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    experiment_id: Mapped[UUID] = mapped_column(
        Uuid,
        ForeignKey("experiments.id", ondelete="CASCADE"),
        nullable=False,
    )
    execution_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    iteration_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    variant: Mapped[str] = mapped_column(String(20), nullable=False)
    repetition: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(100), nullable=True)
    evidence: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    evidence_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
    )

    experiment: Mapped["ExperimentRecord"] = relationship(
        "ExperimentRecord",
        back_populates="iteration_outcomes",
    )


class ExperimentResultRecord(Base):
    """Stores the one immutable complete result for an experiment."""

    __tablename__ = "experiment_results"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    experiment_id: Mapped[UUID] = mapped_column(
        Uuid,
        ForeignKey("experiments.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )
    execution_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    contract_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    result: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
    )

    experiment: Mapped["ExperimentRecord"] = relationship(
        "ExperimentRecord",
        back_populates="result",
    )
