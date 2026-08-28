"""Unit tests for the investigation state machine and transitions."""

import pytest
from tests.fakes.investigation_repository import InMemoryInvestigationRepository
from tests.fakes.runners import FakeRunner

from app.domain.investigation.errors import (
    InvalidStateTransitionError,
    TerminalStateImmutableError,
)
from app.domain.investigation.schemas import (
    InvestigationCreateRequest,
    InvestigationStatus,
)
from app.domain.investigation.service import InvestigationService
from app.domain.runner.schemas import AgentArtifactRef, EnvironmentSliceRef


def _sample_create_request() -> InvestigationCreateRequest:
    return InvestigationCreateRequest(
        trace_ref={"platform": "langsmith", "trace_id": "tr_100"},
        world_ref={"world_id": "support_v1"},
        slice_ref=EnvironmentSliceRef(
            world_id="support_v1",
            world_version="1.0",
            slice_name="refund_dispute",
            fixture_bundle_ref="bundle_1",
            provenance="observed",
        ),
        investigator_ref=AgentArtifactRef(
            kind="prime_profile",
            digest_or_profile="prime@0.2.0",
        ),
    )


@pytest.mark.asyncio
async def test_valid_lifecycle_transitions() -> None:
    """Verify happy path transition: pending -> provisioning -> running -> completed."""
    repo = InMemoryInvestigationRepository()
    service = InvestigationService(repo, runner=FakeRunner())

    # 1. Start creates in pending
    resp, token = await service.start(_sample_create_request())
    assert resp.status == InvestigationStatus.PENDING

    # 2. Transition to provisioning
    rec1 = await service.transition_status(resp.id, InvestigationStatus.PROVISIONING)
    assert rec1.status == InvestigationStatus.PROVISIONING.value

    # 3. Transition to running
    rec2 = await service.transition_status(resp.id, InvestigationStatus.RUNNING)
    assert rec2.status == InvestigationStatus.RUNNING.value
    assert rec2.started_at is not None

    # 4. Transition to completed
    rec3 = await service.transition_status(resp.id, InvestigationStatus.COMPLETED)
    assert rec3.status == InvestigationStatus.COMPLETED.value
    assert rec3.finished_at is not None


@pytest.mark.asyncio
async def test_invalid_transitions_rejected() -> None:
    """Verify invalid transitions raise InvalidStateTransitionError."""
    repo = InMemoryInvestigationRepository()
    service = InvestigationService(repo, runner=FakeRunner())

    resp, _ = await service.start(_sample_create_request())

    # Cannot jump directly from pending to completed or running
    with pytest.raises(InvalidStateTransitionError):
        await service.transition_status(resp.id, InvestigationStatus.COMPLETED)

    with pytest.raises(InvalidStateTransitionError):
        await service.transition_status(resp.id, InvestigationStatus.RUNNING)


@pytest.mark.asyncio
async def test_terminal_states_are_immutable() -> None:
    """Verify terminal states (completed, failed, cancelled) cannot be modified."""
    repo = InMemoryInvestigationRepository()
    service = InvestigationService(repo, runner=FakeRunner())

    resp, _ = await service.start(_sample_create_request())
    await service.transition_status(resp.id, InvestigationStatus.PROVISIONING)
    await service.transition_status(resp.id, InvestigationStatus.RUNNING)
    await service.transition_status(resp.id, InvestigationStatus.COMPLETED)

    # Attempting to move out of completed fails
    with pytest.raises(TerminalStateImmutableError):
        await service.transition_status(resp.id, InvestigationStatus.RUNNING)

    with pytest.raises(TerminalStateImmutableError):
        await service.transition_status(resp.id, InvestigationStatus.FAILED)
