"""PostgreSQL integrity checks for durable experiment persistence."""

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.db import get_session_factory
from app.domain.experiment.contracts import (
    ExperimentContract,
    IterationIdentity,
    build_iteration_plan,
)
from app.domain.experiment.engine import CompletedIteration
from app.domain.experiment.events import ExperimentEvent, ExperimentEventKind
from app.domain.experiment.models import (
    ExperimentIterationOutcomeRecord,
    ExperimentRecord,
)
from app.domain.experiment.repository import SqlAlchemyExperimentRepository
from app.domain.experiment.service import ExperimentService
from app.domain.experiment.state_machine import (
    ExperimentConflictError,
    ExperimentPersistenceError,
    ExperimentStatus,
    IterationOutcomeStatus,
)
from app.domain.simulation.runner import RunVerdict, SimulationRun
from app.domain.simulation.schemas import SimulationState

FIXTURES = Path(__file__).parents[2] / "unit" / "experiment" / "fixtures"
NOW = datetime(2026, 9, 2, tzinfo=UTC)


def database_settings_or_skip() -> Settings:
    """Return disposable database settings or skip without touching a shared database."""
    try:
        settings = Settings()  # type: ignore[call-arg]
    except ValidationError:
        pytest.skip("DATABASE_URL is required for experiment persistence integration tests")
    if settings.environment != "test":
        pytest.skip(
            "DATABASE_URL with ENVIRONMENT=test is required for disposable integration tests"
        )
    return settings


def support_contract(experiment_id: UUID) -> ExperimentContract:
    """Load the reviewed support contract with an integration-local identity."""
    payload = json.loads((FIXTURES / "valid_support_experiment.json").read_text(encoding="utf-8"))
    payload["experiment_id"] = str(experiment_id)
    return ExperimentContract.model_validate(payload)


def completed_run(contract: ExperimentContract) -> SimulationRun:
    """Build serializable completed evidence without testing the runner here."""
    return SimulationRun(
        run_id=uuid4(),
        bundle_id=uuid4(),
        bundle_content_hash=contract.scenario.content_hash,
        scenario_id=contract.scenario.scenario_id,
        verdict=RunVerdict.ACCEPTED,
        final_state=SimulationState(),
        completed_at=NOW.isoformat(),
    )


async def delete_experiments(experiment_ids: tuple[UUID, ...]) -> None:
    """Remove only test-local experiments and their cascading persistence records."""
    async with get_session_factory()() as session:
        await session.execute(
            delete(ExperimentRecord).where(ExperimentRecord.id.in_(experiment_ids))
        )
        await session.commit()


class CoordinatedExperimentRepository(SqlAlchemyExperimentRepository):
    """Pause real repository writes so separate sessions reach the database race."""

    def __init__(
        self,
        session: AsyncSession,
        *,
        transition_barrier: asyncio.Barrier | None = None,
        outcome_barrier: asyncio.Barrier | None = None,
    ) -> None:
        super().__init__(session)
        self._transition_barrier = transition_barrier
        self._outcome_barrier = outcome_barrier

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
        if self._transition_barrier is not None:
            await self._transition_barrier.wait()
        return await super().transition(
            experiment_id,
            expected_status=expected_status,
            expected_execution_id=expected_execution_id,
            status=status,
            execution_id=execution_id,
            error_code=error_code,
            started_at=started_at,
            finished_at=finished_at,
        )

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
        if self._outcome_barrier is not None:
            await self._outcome_barrier.wait()
        return await super().record_iteration_outcome(
            experiment_id,
            execution_id,
            identity,
            status=status,
            error_code=error_code,
            evidence=evidence,
            evidence_hash=evidence_hash,
        )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_cross_session_transition_and_iteration_report_races_are_safe() -> None:
    """A stale start loses conditionally; identical reports replay and changed ones conflict."""
    database_settings_or_skip()
    experiment_id = uuid4()
    contract = support_contract(experiment_id)
    session_factory = get_session_factory()

    try:
        async with session_factory() as session:
            service = ExperimentService(SqlAlchemyExperimentRepository(session), clock=lambda: NOW)
            await service.create(contract)
            await session.commit()

        start_barrier = asyncio.Barrier(2)

        async def start(execution_id: UUID) -> ExperimentRecord | Exception:
            async with session_factory() as session:
                service = ExperimentService(
                    CoordinatedExperimentRepository(
                        session,
                        transition_barrier=start_barrier,
                    ),
                    clock=lambda: NOW,
                )
                try:
                    started = await service.start_execution(experiment_id, execution_id)
                except ExperimentConflictError as error:
                    await session.rollback()
                    return error
                await session.commit()
                return started

        starts = await asyncio.gather(start(uuid4()), start(uuid4()))
        winners = [result for result in starts if isinstance(result, ExperimentRecord)]
        losers = [result for result in starts if isinstance(result, ExperimentConflictError)]
        assert len(winners) == 1
        assert len(losers) == 1
        execution_id = winners[0].execution_id
        assert execution_id is not None

        async def report(
            identity: IterationIdentity,
            run: SimulationRun,
            barrier: asyncio.Barrier,
        ) -> ExperimentIterationOutcomeRecord | Exception:
            async with session_factory() as session:
                service = ExperimentService(
                    CoordinatedExperimentRepository(session, outcome_barrier=barrier),
                    clock=lambda: NOW,
                )
                try:
                    persisted = await service.record_iteration_outcome(
                        experiment_id,
                        execution_id,
                        CompletedIteration(identity=identity, result=run),
                    )
                except ExperimentConflictError as error:
                    await session.rollback()
                    return error
                await session.commit()
                return persisted

        identities = build_iteration_plan(contract).iterations
        identical_run = completed_run(contract)
        identical_barrier = asyncio.Barrier(2)
        identical_reports = await asyncio.gather(
            report(identities[0], identical_run, identical_barrier),
            report(identities[0], identical_run, identical_barrier),
        )
        assert all(
            isinstance(report, ExperimentIterationOutcomeRecord) for report in identical_reports
        )
        assert identical_reports[0].id == identical_reports[1].id

        first_barrier = asyncio.Barrier(2)
        changed_run = identical_run.model_copy(
            update={"completed_at": NOW.replace(year=2027).isoformat()}
        )
        conflicting_reports = await asyncio.gather(
            report(identities[1], completed_run(contract), first_barrier),
            report(identities[1], changed_run, first_barrier),
        )
        assert sum(
            isinstance(report, ExperimentIterationOutcomeRecord) for report in conflicting_reports
        ) == 1
        assert sum(
            isinstance(report, ExperimentConflictError) for report in conflicting_reports
        ) == 1
    finally:
        await delete_experiments((experiment_id,))


@pytest.mark.integration
@pytest.mark.asyncio
async def test_conflicting_event_replay_is_rejected_and_iteration_events_are_planned() -> None:
    """A sequence only accepts an exact retransmission and a scheduled iteration identity."""
    database_settings_or_skip()
    experiment_id = uuid4()
    execution_id = uuid4()
    contract = support_contract(experiment_id)

    try:
        async with get_session_factory()() as session:
            service = ExperimentService(SqlAlchemyExperimentRepository(session), clock=lambda: NOW)
            await service.create(contract)
            await service.start_execution(experiment_id, execution_id)
            original = ExperimentEvent(
                experiment_id=experiment_id,
                execution_id=execution_id,
                sequence=1,
                kind=ExperimentEventKind.PLAN_CREATED,
                emitted_at=NOW,
            )
            assert await service.record_events(experiment_id, execution_id, [original]) == 1
            assert await service.record_events(experiment_id, execution_id, [original]) == 0

            conflicting = original.model_copy(update={"kind": ExperimentEventKind.LIMIT_REACHED})
            with pytest.raises(ExperimentConflictError, match="canonical content"):
                await service.record_events(experiment_id, execution_id, [conflicting])

            unplanned_identity = build_iteration_plan(contract).iterations[0].model_copy(
                update={"ordinal": 999}
            )
            unplanned = ExperimentEvent(
                experiment_id=experiment_id,
                execution_id=execution_id,
                sequence=2,
                kind=ExperimentEventKind.ITERATION_STARTED,
                emitted_at=NOW,
                iteration=unplanned_identity,
            )
            with pytest.raises(ExperimentPersistenceError, match="schedule"):
                await service.record_events(experiment_id, execution_id, [unplanned])
            await session.commit()
    finally:
        await delete_experiments((experiment_id,))
