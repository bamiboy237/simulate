"""Repository operations for durable support-experiment persistence."""

import json
from datetime import datetime, timezone
from typing import Any, Protocol
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.experiment.contracts import IterationIdentity
from app.domain.experiment.events import ExperimentEvent
from app.domain.experiment.models import (
    ExperimentEventRecord,
    ExperimentIterationOutcomeRecord,
    ExperimentRecord,
    ExperimentResultRecord,
)
from app.domain.experiment.state_machine import (
    ExperimentConflictError,
    ExperimentStatus,
    IterationOutcomeStatus,
)


class ExperimentRepository(Protocol):
    """Persistence boundary used by ExperimentService."""

    async def create(
        self,
        *,
        experiment_id: UUID,
        contract: dict[str, Any],
        contract_hash: str,
        status: ExperimentStatus,
    ) -> ExperimentRecord: ...

    async def get_by_id(self, experiment_id: UUID) -> ExperimentRecord | None: ...

    async def transition(
        self,
        experiment_id: UUID,
        *,
        expected_status: ExperimentStatus,
        expected_execution_id: UUID | None,
        status: ExperimentStatus,
        execution_id: UUID | None,
        error_code: str | None,
        started_at: datetime | None,
        finished_at: datetime | None,
    ) -> ExperimentRecord | None: ...

    async def record_events(self, events: list[ExperimentEvent]) -> int: ...

    async def get_events(
        self,
        experiment_id: UUID,
        execution_id: UUID,
        *,
        after_sequence: int = 0,
        limit: int = 100,
    ) -> list[ExperimentEventRecord]: ...

    async def record_iteration_outcome(
        self,
        experiment_id: UUID,
        execution_id: UUID,
        identity: IterationIdentity,
        *,
        status: IterationOutcomeStatus,
        error_code: str | None,
        evidence: dict[str, Any] | None,
        evidence_hash: str | None,
    ) -> ExperimentIterationOutcomeRecord: ...

    async def get_iteration_outcomes(
        self,
        experiment_id: UUID,
        execution_id: UUID,
    ) -> list[ExperimentIterationOutcomeRecord]: ...

    async def publish_result(
        self,
        experiment_id: UUID,
        execution_id: UUID,
        *,
        contract_hash: str,
        content_hash: str,
        result: dict[str, Any],
        completed_at: datetime,
    ) -> ExperimentResultRecord | None: ...

    async def get_result_record(
        self,
        experiment_id: UUID,
    ) -> ExperimentResultRecord | None: ...


class SqlAlchemyExperimentRepository:
    """PostgreSQL and SQLAlchemy implementation of ExperimentRepository."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(
        self,
        *,
        experiment_id: UUID,
        contract: dict[str, Any],
        contract_hash: str,
        status: ExperimentStatus,
    ) -> ExperimentRecord:
        record = ExperimentRecord(
            id=experiment_id,
            contract=contract,
            contract_hash=contract_hash,
            status=status.value,
            created_at=datetime.now(timezone.utc),
        )
        self._session.add(record)
        await self._session.flush()
        return record

    async def get_by_id(self, experiment_id: UUID) -> ExperimentRecord | None:
        result = await self._session.execute(
            select(ExperimentRecord).where(ExperimentRecord.id == experiment_id)
        )
        return result.scalar_one_or_none()

    async def transition(
        self,
        experiment_id: UUID,
        *,
        expected_status: ExperimentStatus,
        expected_execution_id: UUID | None,
        status: ExperimentStatus,
        execution_id: UUID | None,
        error_code: str | None,
        started_at: datetime | None,
        finished_at: datetime | None,
    ) -> ExperimentRecord | None:
        """Apply one lifecycle update only while its exact prior state remains true."""
        conditions = [
            ExperimentRecord.id == experiment_id,
            ExperimentRecord.status == expected_status.value,
        ]
        if expected_execution_id is None:
            conditions.append(ExperimentRecord.execution_id.is_(None))
        else:
            conditions.append(ExperimentRecord.execution_id == expected_execution_id)

        values: dict[str, Any] = {
            "status": status.value,
            "execution_id": execution_id,
            "error_code": error_code,
        }
        if started_at is not None:
            values["started_at"] = started_at
        if finished_at is not None:
            values["finished_at"] = finished_at

        result = await self._session.execute(
            update(ExperimentRecord)
            .where(*conditions)
            .values(**values)
            .returning(ExperimentRecord)
        )
        return result.scalar_one_or_none()

    async def record_events(self, events: list[ExperimentEvent]) -> int:
        """Insert exact retransmissions idempotently and reject changed replays."""
        if not events:
            return 0

        first = events[0]
        canonical_by_sequence: dict[int, ExperimentEvent] = {}
        for event in events:
            if (
                event.experiment_id != first.experiment_id
                or event.execution_id != first.execution_id
            ):
                raise ExperimentConflictError(
                    "event batch contains more than one experiment execution"
                )
            previous = canonical_by_sequence.setdefault(event.sequence, event)
            if _canonical_value(event.model_dump(mode="json")) != _canonical_value(
                previous.model_dump(mode="json")
            ):
                raise ExperimentConflictError(
                    "event sequence is replayed with different canonical content"
                )

        await self._lock_running_attempt(first.experiment_id, first.execution_id)
        sequences = tuple(canonical_by_sequence)
        existing_rows = await self._session.execute(
            select(ExperimentEventRecord).where(
                ExperimentEventRecord.experiment_id == first.experiment_id,
                ExperimentEventRecord.execution_id == first.execution_id,
                ExperimentEventRecord.sequence.in_(sequences),
            )
        )
        existing_by_sequence = {
            record.sequence: record for record in existing_rows.scalars().all()
        }
        for sequence, event in canonical_by_sequence.items():
            existing = existing_by_sequence.get(sequence)
            if existing is not None and _canonical_value(existing.payload) != _canonical_value(
                event.model_dump(mode="json")
            ):
                raise ExperimentConflictError(
                    "event sequence is replayed with different canonical content"
                )

        values = [
            {
                "experiment_id": event.experiment_id,
                "execution_id": event.execution_id,
                "sequence": event.sequence,
                "kind": event.kind.value,
                "payload": event.model_dump(mode="json"),
                "emitted_at": event.emitted_at,
            }
            for sequence, event in canonical_by_sequence.items()
            if sequence not in existing_by_sequence
        ]
        if not values:
            return 0

        statement = (
            pg_insert(ExperimentEventRecord)
            .values(values)
            .on_conflict_do_nothing(
                constraint="uq_experiment_events_execution_sequence"
            )
            .returning(ExperimentEventRecord.id)
        )
        result = await self._session.execute(statement)
        return len(result.scalars().all())

    async def get_events(
        self,
        experiment_id: UUID,
        execution_id: UUID,
        *,
        after_sequence: int = 0,
        limit: int = 100,
    ) -> list[ExperimentEventRecord]:
        statement = (
            select(ExperimentEventRecord)
            .where(
                ExperimentEventRecord.experiment_id == experiment_id,
                ExperimentEventRecord.execution_id == execution_id,
                ExperimentEventRecord.sequence > after_sequence,
            )
            .order_by(ExperimentEventRecord.sequence.asc())
            .limit(limit)
        )
        result = await self._session.execute(statement)
        return list(result.scalars().all())

    async def record_iteration_outcome(
        self,
        experiment_id: UUID,
        execution_id: UUID,
        identity: IterationIdentity,
        *,
        status: IterationOutcomeStatus,
        error_code: str | None,
        evidence: dict[str, Any] | None,
        evidence_hash: str | None,
    ) -> ExperimentIterationOutcomeRecord:
        """Store one immutable planned outcome, returning an exact retransmission."""
        await self._lock_running_attempt(experiment_id, execution_id)
        statement = select(ExperimentIterationOutcomeRecord).where(
            ExperimentIterationOutcomeRecord.experiment_id == experiment_id,
            ExperimentIterationOutcomeRecord.execution_id == execution_id,
            ExperimentIterationOutcomeRecord.iteration_id == identity.iteration_id,
        )
        existing = (await self._session.execute(statement)).scalar_one_or_none()
        if existing is not None:
            return existing

        result = await self._session.execute(
            pg_insert(ExperimentIterationOutcomeRecord)
            .values(
                experiment_id=experiment_id,
                execution_id=execution_id,
                iteration_id=identity.iteration_id,
                ordinal=identity.ordinal,
                variant=identity.variant.value,
                repetition=identity.repetition,
                status=status.value,
                error_code=error_code,
                evidence=evidence,
                evidence_hash=evidence_hash,
                recorded_at=datetime.now(timezone.utc),
            )
            .on_conflict_do_nothing(
                constraint="uq_experiment_iteration_outcomes_execution_iteration"
            )
            .returning(ExperimentIterationOutcomeRecord)
        )
        inserted = result.scalar_one_or_none()
        if inserted is not None:
            return inserted

        existing = (await self._session.execute(statement)).scalar_one_or_none()
        if existing is None:
            raise ExperimentConflictError("iteration outcome was not persisted")
        return existing

    async def get_iteration_outcomes(
        self,
        experiment_id: UUID,
        execution_id: UUID,
    ) -> list[ExperimentIterationOutcomeRecord]:
        statement = (
            select(ExperimentIterationOutcomeRecord)
            .where(
                ExperimentIterationOutcomeRecord.experiment_id == experiment_id,
                ExperimentIterationOutcomeRecord.execution_id == execution_id,
            )
            .order_by(ExperimentIterationOutcomeRecord.ordinal.asc())
        )
        result = await self._session.execute(statement)
        return list(result.scalars().all())

    async def publish_result(
        self,
        experiment_id: UUID,
        execution_id: UUID,
        *,
        contract_hash: str,
        content_hash: str,
        result: dict[str, Any],
        completed_at: datetime,
    ) -> ExperimentResultRecord | None:
        """Store a result only if the matching running attempt can complete."""
        try:
            async with self._session.begin_nested():
                inserted = await self._session.execute(
                    pg_insert(ExperimentResultRecord)
                    .values(
                        experiment_id=experiment_id,
                        execution_id=execution_id,
                        contract_hash=contract_hash,
                        content_hash=content_hash,
                        result=result,
                        created_at=completed_at,
                    )
                    .on_conflict_do_nothing(constraint="uq_experiment_results_experiment")
                    .returning(ExperimentResultRecord)
                )
                persisted_result = inserted.scalar_one_or_none()
                if persisted_result is None:
                    raise _ResultPublicationConflict

                updated = await self._session.execute(
                    update(ExperimentRecord)
                    .where(
                        ExperimentRecord.id == experiment_id,
                        ExperimentRecord.status == ExperimentStatus.RUNNING.value,
                        ExperimentRecord.execution_id == execution_id,
                    )
                    .values(
                        status=ExperimentStatus.COMPLETED.value,
                        error_code=None,
                        finished_at=completed_at,
                    )
                    .returning(ExperimentRecord.id)
                )
                if updated.scalar_one_or_none() is None:
                    raise _ResultPublicationConflict
        except _ResultPublicationConflict:
            return None
        return persisted_result

    async def get_result_record(
        self,
        experiment_id: UUID,
    ) -> ExperimentResultRecord | None:
        result = await self._session.execute(
            select(ExperimentResultRecord).where(
                ExperimentResultRecord.experiment_id == experiment_id
            )
        )
        return result.scalar_one_or_none()

    async def _lock_running_attempt(
        self,
        experiment_id: UUID,
        execution_id: UUID,
    ) -> None:
        locked = await self._session.execute(
            select(ExperimentRecord.id)
            .where(
                ExperimentRecord.id == experiment_id,
                ExperimentRecord.status == ExperimentStatus.RUNNING.value,
                ExperimentRecord.execution_id == execution_id,
            )
            .with_for_update()
        )
        if locked.scalar_one_or_none() is None:
            raise ExperimentConflictError(
                "experiment is no longer the matching running execution"
            )


class _ResultPublicationConflict(Exception):
    """Internal sentinel used to roll back an unsuccessful publication savepoint."""


def _canonical_value(value: dict[str, Any]) -> str:
    """Serialize persisted JSON through its unique canonical representation."""
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
