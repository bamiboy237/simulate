"""SQLAlchemy ORM models for investigation persistence."""

from datetime import datetime, timezone
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import BigInteger, DateTime, ForeignKey, Index, String, Text, UniqueConstraint, Uuid
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


class InvestigationRecord(Base):
    """Stores the lifecycle and metadata of one investigation."""

    __tablename__ = "investigations"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    status: Mapped[str] = mapped_column(String(30), default="pending", index=True)
    trace_ref: Mapped[dict[str, Any]] = mapped_column(JSONB)
    world_ref: Mapped[dict[str, Any]] = mapped_column(JSONB)
    slice_ref: Mapped[dict[str, Any]] = mapped_column(JSONB)
    investigator_ref: Mapped[dict[str, Any]] = mapped_column(JSONB)
    tested_agent_ref: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    task_brief: Mapped[str] = mapped_column(Text)
    bridge_token_hash: Mapped[str] = mapped_column(String(64), index=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_heartbeat_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
    )
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    events: Mapped[list["InvestigationEventRecord"]] = relationship(
        "InvestigationEventRecord",
        back_populates="investigation",
        cascade="all, delete-orphan",
        order_by="InvestigationEventRecord.seq",
    )
    messages: Mapped[list["InvestigationMessageRecord"]] = relationship(
        "InvestigationMessageRecord",
        back_populates="investigation",
        cascade="all, delete-orphan",
        order_by="InvestigationMessageRecord.id",
    )
    summary: Mapped["InvestigationSummaryRecord | None"] = relationship(
        "InvestigationSummaryRecord",
        back_populates="investigation",
        uselist=False,
        cascade="all, delete-orphan",
    )


class InvestigationEventRecord(Base):
    """Stores a single emitted event from an investigation."""

    __tablename__ = "investigation_events"
    __table_args__ = (
        UniqueConstraint("investigation_id", "seq", name="uq_investigation_events_seq"),
        Index("ix_investigation_events_inv_seq", "investigation_id", "seq"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    investigation_id: Mapped[UUID] = mapped_column(
        Uuid,
        ForeignKey("investigations.id", ondelete="CASCADE"),
    )
    seq: Mapped[int] = mapped_column(nullable=False)
    type: Mapped[str] = mapped_column(String(50), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    emitted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )

    investigation: Mapped["InvestigationRecord"] = relationship(
        "InvestigationRecord",
        back_populates="events",
    )


class InvestigationMessageRecord(Base):
    """Stores chat messages sent between the user and investigator."""

    __tablename__ = "investigation_messages"
    __table_args__ = (
        Index("ix_investigation_messages_inv", "investigation_id", "id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    investigation_id: Mapped[UUID] = mapped_column(
        Uuid,
        ForeignKey("investigations.id", ondelete="CASCADE"),
    )
    sender: Mapped[str] = mapped_column(String(20), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    mode: Mapped[str | None] = mapped_column(String(20), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
    )

    investigation: Mapped["InvestigationRecord"] = relationship(
        "InvestigationRecord",
        back_populates="messages",
    )


class InvestigationSummaryRecord(Base):
    """Stores the final findings and next steps of a completed investigation."""

    __tablename__ = "investigation_summaries"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    investigation_id: Mapped[UUID] = mapped_column(
        Uuid,
        ForeignKey("investigations.id", ondelete="CASCADE"),
        unique=True,
    )
    findings: Mapped[str] = mapped_column(Text, nullable=False)
    next_step: Mapped[str] = mapped_column(Text, nullable=False)
    evidence_refs: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
    )

    investigation: Mapped["InvestigationRecord"] = relationship(
        "InvestigationRecord",
        back_populates="summary",
    )
