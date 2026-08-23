"""Repository implementation for investigation persistence."""

from datetime import datetime, timezone
from typing import Any, Protocol
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.investigation.models import (
    InvestigationEventRecord,
    InvestigationMessageRecord,
    InvestigationRecord,
    InvestigationSummaryRecord,
)
from app.domain.runner.schemas import RunnerEvent


class InvestigationRepository(Protocol):
    """Protocol defining persistence operations for investigations."""

    async def create(
        self,
        *,
        investigation_id: UUID,
        status: str,
        trace_ref: dict[str, Any],
        world_ref: dict[str, Any],
        slice_ref: dict[str, Any],
        investigator_ref: dict[str, Any],
        tested_agent_ref: dict[str, Any] | None,
        task_brief: str,
        bridge_token_hash: str,
    ) -> InvestigationRecord: ...

    async def get_by_id(self, investigation_id: UUID) -> InvestigationRecord | None: ...

    async def get_by_bridge_token_hash(self, token_hash: str) -> InvestigationRecord | None: ...

    async def list_investigations(
        self,
        *,
        limit: int = 50,
        offset: int = 0,
    ) -> list[InvestigationRecord]: ...

    async def update_status(
        self,
        investigation_id: UUID,
        status: str,
        *,
        error: str | None = None,
        started_at: datetime | None = None,
        finished_at: datetime | None = None,
    ) -> InvestigationRecord | None: ...

    async def update_heartbeat(
        self,
        investigation_id: UUID,
        timestamp: datetime,
    ) -> None: ...

    async def record_events(
        self,
        investigation_id: UUID,
        events: list[RunnerEvent],
    ) -> int: ...

    async def get_events(
        self,
        investigation_id: UUID,
        *,
        after_seq: int = 0,
        limit: int = 100,
    ) -> list[InvestigationEventRecord]: ...

    async def record_message(
        self,
        investigation_id: UUID,
        *,
        sender: str,
        body: str,
        mode: str | None = None,
    ) -> InvestigationMessageRecord: ...

    async def get_messages(
        self,
        investigation_id: UUID,
        *,
        after_id: int = 0,
        limit: int = 50,
    ) -> list[InvestigationMessageRecord]: ...

    async def save_summary(
        self,
        investigation_id: UUID,
        *,
        findings: str,
        next_step: str,
        evidence_refs: list[dict[str, Any]],
    ) -> InvestigationSummaryRecord: ...

    async def get_summary(
        self,
        investigation_id: UUID,
    ) -> InvestigationSummaryRecord | None: ...

    async def find_stale_running(
        self,
        cutoff: datetime,
    ) -> list[InvestigationRecord]: ...


class SqlAlchemyInvestigationRepository:
    """PostgreSQL and SQLAlchemy implementation of InvestigationRepository."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(
        self,
        *,
        investigation_id: UUID,
        status: str,
        trace_ref: dict[str, Any],
        world_ref: dict[str, Any],
        slice_ref: dict[str, Any],
        investigator_ref: dict[str, Any],
        tested_agent_ref: dict[str, Any] | None,
        task_brief: str,
        bridge_token_hash: str,
    ) -> InvestigationRecord:
        record = InvestigationRecord(
            id=investigation_id,
            status=status,
            trace_ref=trace_ref,
            world_ref=world_ref,
            slice_ref=slice_ref,
            investigator_ref=investigator_ref,
            tested_agent_ref=tested_agent_ref,
            task_brief=task_brief,
            bridge_token_hash=bridge_token_hash,
            created_at=datetime.now(timezone.utc),
        )
        self._session.add(record)
        await self._session.flush()
        return record

    async def get_by_id(self, investigation_id: UUID) -> InvestigationRecord | None:
        stmt = select(InvestigationRecord).where(InvestigationRecord.id == investigation_id)
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_by_bridge_token_hash(self, token_hash: str) -> InvestigationRecord | None:
        stmt = select(InvestigationRecord).where(
            InvestigationRecord.bridge_token_hash == token_hash
        )
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def list_investigations(
        self,
        *,
        limit: int = 50,
        offset: int = 0,
    ) -> list[InvestigationRecord]:
        stmt = (
            select(InvestigationRecord)
            .order_by(InvestigationRecord.created_at.desc())
            .offset(offset)
            .limit(limit)
        )
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def update_status(
        self,
        investigation_id: UUID,
        status: str,
        *,
        error: str | None = None,
        started_at: datetime | None = None,
        finished_at: datetime | None = None,
    ) -> InvestigationRecord | None:
        record = await self.get_by_id(investigation_id)
        if record is None:
            return None

        record.status = status
        if error is not None:
            record.error = error
        if started_at is not None:
            record.started_at = started_at
        if finished_at is not None:
            record.finished_at = finished_at

        await self._session.flush()
        return record

    async def update_heartbeat(
        self,
        investigation_id: UUID,
        timestamp: datetime,
    ) -> None:
        stmt = (
            update(InvestigationRecord)
            .where(InvestigationRecord.id == investigation_id)
            .values(last_heartbeat_at=timestamp)
        )
        await self._session.execute(stmt)
        await self._session.flush()

    async def record_events(
        self,
        investigation_id: UUID,
        events: list[RunnerEvent],
    ) -> int:
        """Insert events idempotently. Existing (investigation_id, seq) pairs are ignored."""
        if not events:
            return 0

        inserted_count = 0
        bind = self._session.bind
        dialect_name = bind.dialect.name if bind else "postgresql"

        if dialect_name == "postgresql":
            values = [
                {
                    "investigation_id": investigation_id,
                    "seq": event.seq,
                    "type": str(event.type),
                    "payload": event.payload,
                    "emitted_at": event.emitted_at,
                }
                for event in events
            ]
            stmt = pg_insert(InvestigationEventRecord).values(values)
            stmt = stmt.on_conflict_do_nothing(
                constraint="uq_investigation_events_seq",
            )
            result = await self._session.execute(stmt)
            rowcount = getattr(result, "rowcount", None)
            if rowcount is not None and isinstance(rowcount, int) and rowcount >= 0:
                inserted_count = rowcount
            else:
                inserted_count = len(events)
        else:
            # Fallback for SQLite / other dialects in unit tests
            for event in events:
                existing = await self._session.execute(
                    select(InvestigationEventRecord.id).where(
                        InvestigationEventRecord.investigation_id == investigation_id,
                        InvestigationEventRecord.seq == event.seq,
                    )
                )
                if existing.scalar_one_or_none() is None:
                    rec = InvestigationEventRecord(
                        investigation_id=investigation_id,
                        seq=event.seq,
                        type=str(event.type),
                        payload=event.payload,
                        emitted_at=event.emitted_at,
                    )
                    self._session.add(rec)
                    inserted_count += 1

        await self._session.flush()
        return inserted_count

    async def get_events(
        self,
        investigation_id: UUID,
        *,
        after_seq: int = 0,
        limit: int = 100,
    ) -> list[InvestigationEventRecord]:
        stmt = (
            select(InvestigationEventRecord)
            .where(
                InvestigationEventRecord.investigation_id == investigation_id,
                InvestigationEventRecord.seq > after_seq,
            )
            .order_by(InvestigationEventRecord.seq.asc())
            .limit(limit)
        )
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def record_message(
        self,
        investigation_id: UUID,
        *,
        sender: str,
        body: str,
        mode: str | None = None,
    ) -> InvestigationMessageRecord:
        msg = InvestigationMessageRecord(
            investigation_id=investigation_id,
            sender=sender,
            body=body,
            mode=mode,
            created_at=datetime.now(timezone.utc),
        )
        self._session.add(msg)
        await self._session.flush()
        return msg

    async def get_messages(
        self,
        investigation_id: UUID,
        *,
        after_id: int = 0,
        limit: int = 50,
    ) -> list[InvestigationMessageRecord]:
        stmt = (
            select(InvestigationMessageRecord)
            .where(
                InvestigationMessageRecord.investigation_id == investigation_id,
                InvestigationMessageRecord.id > after_id,
            )
            .order_by(InvestigationMessageRecord.id.asc())
            .limit(limit)
        )
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def save_summary(
        self,
        investigation_id: UUID,
        *,
        findings: str,
        next_step: str,
        evidence_refs: list[dict[str, Any]],
    ) -> InvestigationSummaryRecord:
        summary = InvestigationSummaryRecord(
            investigation_id=investigation_id,
            findings=findings,
            next_step=next_step,
            evidence_refs=evidence_refs,
            created_at=datetime.now(timezone.utc),
        )
        self._session.add(summary)
        await self._session.flush()
        return summary

    async def get_summary(
        self,
        investigation_id: UUID,
    ) -> InvestigationSummaryRecord | None:
        stmt = select(InvestigationSummaryRecord).where(
            InvestigationSummaryRecord.investigation_id == investigation_id
        )
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def find_stale_running(
        self,
        cutoff: datetime,
    ) -> list[InvestigationRecord]:
        stmt = select(InvestigationRecord).where(
            InvestigationRecord.status == "running",
            (InvestigationRecord.last_heartbeat_at < cutoff)
            | (
                (InvestigationRecord.last_heartbeat_at.is_(None))
                & (InvestigationRecord.started_at < cutoff)
            ),
        )
        result = await self._session.execute(stmt)
        return list(result.scalars().all())
