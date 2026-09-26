"""Focused unit tests for durable experiment lifecycle rules."""

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest

from app.domain.experiment.contracts import (
    ExperimentContract,
    IterationIdentity,
    build_iteration_plan,
)
from app.domain.experiment.models import ExperimentRecord
from app.domain.experiment.service import ExperimentService
from app.domain.experiment.state_machine import (
    ExperimentPersistenceError,
    ExperimentStatus,
    InvalidExperimentTransitionError,
    TerminalExperimentImmutableError,
)

FIXTURES = Path(__file__).with_name("fixtures")
NOW = datetime(2026, 9, 2, tzinfo=UTC)


class InMemoryExperimentRepository:
    """Small repository double that exposes only persistence-service behavior."""

    def __init__(self) -> None:
        self.records: dict[UUID, ExperimentRecord] = {}

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
            created_at=NOW,
        )
        self.records[experiment_id] = record
        return record

    async def get_by_id(self, experiment_id: UUID) -> ExperimentRecord | None:
        return self.records.get(experiment_id)

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
        record = self.records.get(experiment_id)
        if (
            record is None
            or record.status != expected_status.value
            or record.execution_id != expected_execution_id
        ):
            return None
        record.status = status.value
        record.execution_id = execution_id
        record.error_code = error_code
        if started_at is not None:
            record.started_at = started_at
        if finished_at is not None:
            record.finished_at = finished_at
        return record

def support_contract(*, experiment_id: UUID | None = None) -> ExperimentContract:
    """Load the reviewed support contract with an independent experiment identity."""
    payload = json.loads((FIXTURES / "valid_support_experiment.json").read_text(encoding="utf-8"))
    payload["experiment_id"] = str(experiment_id or uuid4())
    return ExperimentContract.model_validate(payload)


async def create_running_experiment(
    service: ExperimentService,
    contract: ExperimentContract,
) -> tuple[UUID, tuple[IterationIdentity, ...]]:
    """Persist a contract and start its one execution attempt."""
    await service.create(contract)
    execution_id = uuid4()
    await service.start_execution(contract.experiment_id, execution_id)
    return execution_id, build_iteration_plan(contract).iterations


@pytest.mark.asyncio
async def test_service_controls_lifecycle_failure_cancellation_and_terminal_immutability() -> None:
    """Only the service can make legal lifecycle changes and persist safe failures."""
    repository = InMemoryExperimentRepository()
    service = ExperimentService(repository, clock=lambda: NOW)

    cancelled_contract = support_contract()
    await service.create(cancelled_contract)
    cancelled = await service.cancel(cancelled_contract.experiment_id, None)
    assert cancelled.status == ExperimentStatus.CANCELLED.value
    assert cancelled.finished_at == NOW
    with pytest.raises(TerminalExperimentImmutableError):
        await service.fail(
            cancelled_contract.experiment_id,
            None,
            error_code="runner_disconnected",
        )

    failed_contract = support_contract()
    execution_id, _ = await create_running_experiment(service, failed_contract)
    with pytest.raises(ExperimentPersistenceError, match="error_code"):
        await service.fail(
            failed_contract.experiment_id,
            execution_id,
            error_code="database error: connection refused",
        )
    with pytest.raises(InvalidExperimentTransitionError):
        await service.start_execution(failed_contract.experiment_id, uuid4())

    failed = await service.fail(
        failed_contract.experiment_id,
        execution_id,
        error_code="runner_disconnected",
    )
    assert failed.status == ExperimentStatus.FAILED.value
    assert failed.error_code == "runner_disconnected"
    assert failed.finished_at == NOW
