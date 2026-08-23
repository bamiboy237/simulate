"""Unit tests for InvestigationService methods, tokens, idempotent events, and stale sweep."""

from datetime import datetime, timedelta, timezone

import pytest
from tests.fakes.investigation_repository import InMemoryInvestigationRepository

from app.domain.investigation.errors import InvalidBridgeTokenError
from app.domain.investigation.schemas import (
    InvestigationCreateRequest,
    InvestigationStatus,
)
from app.domain.investigation.service import InvestigationService
from app.domain.runner.schemas import (
    AgentArtifactRef,
    EnvironmentSliceRef,
    EventType,
    RunnerEvent,
)


def _sample_create_request() -> InvestigationCreateRequest:
    return InvestigationCreateRequest(
        trace_ref={"platform": "langsmith", "trace_id": "tr_200"},
        world_ref={"world_id": "support_v1"},
        slice_ref=EnvironmentSliceRef(
            world_id="support_v1",
            world_version="1.0",
            slice_name="auth_failure",
            fixture_bundle_ref="bundle_2",
            provenance="observed",
        ),
        investigator_ref=AgentArtifactRef(
            kind="prime_profile",
            digest_or_profile="prime@0.2.0",
        ),
    )


@pytest.mark.asyncio
async def test_token_generation_and_verification() -> None:
    """Verify bearer tokens verify correctly and bad tokens are rejected."""
    repo = InMemoryInvestigationRepository()
    service = InvestigationService(repo)

    resp, token = await service.start(_sample_create_request())
    assert token
    assert resp.bridge_token == token

    # Verification succeeds with valid token
    rec = await service.verify_bridge_token(token)
    assert rec.id == resp.id

    # Invalid token raises error
    with pytest.raises(InvalidBridgeTokenError):
        await service.verify_bridge_token("invalid_token_string")


@pytest.mark.asyncio
async def test_idempotent_event_ingestion_and_heartbeat() -> None:
    """Verify events can be posted repeatedly without duplicating and update heartbeats."""
    repo = InMemoryInvestigationRepository()
    service = InvestigationService(repo)

    resp, token = await service.start(_sample_create_request())
    await service.transition_status(resp.id, InvestigationStatus.PROVISIONING)
    await service.transition_status(resp.id, InvestigationStatus.RUNNING)

    now = datetime.now(timezone.utc)
    events = [
        RunnerEvent(
            investigation_id=resp.id,
            seq=1,
            type=EventType.investigation_started,
            payload={},
            emitted_at=now,
        ),
        RunnerEvent(
            investigation_id=resp.id,
            seq=2,
            type=EventType.heartbeat,
            payload={},
            emitted_at=now,
        ),
    ]

    # First ingest
    inserted1 = await service.record_events(token, events)
    assert inserted1 == 2

    # Second ingest of identical batch is idempotent (0 new inserts)
    inserted2 = await service.record_events(token, events)
    assert inserted2 == 0

    # Heartbeat was recorded
    detail = await service.get_by_id(resp.id)
    assert detail.last_heartbeat_at is not None


@pytest.mark.asyncio
async def test_summary_submitted_event_completes_investigation() -> None:
    """Verify summary_submitted event stores findings and sets status to COMPLETED."""
    repo = InMemoryInvestigationRepository()
    service = InvestigationService(repo)

    resp, token = await service.start(_sample_create_request())
    await service.transition_status(resp.id, InvestigationStatus.PROVISIONING)
    await service.transition_status(resp.id, InvestigationStatus.RUNNING)

    summary_event = RunnerEvent(
        investigation_id=resp.id,
        seq=1,
        type=EventType.summary_submitted,
        payload={
            "findings": "Missing idempotency key in refund tool",
            "next_step": "Add request ID parameter to payment endpoint",
            "evidence_refs": [{"event_id": "ev_1"}],
        },
        emitted_at=datetime.now(timezone.utc),
    )

    await service.record_events(token, [summary_event])

    detail = await service.get_by_id(resp.id)
    assert detail.status == InvestigationStatus.COMPLETED
    assert detail.summary is not None
    assert detail.summary.findings == "Missing idempotency key in refund tool"
    assert detail.summary.next_step == "Add request ID parameter to payment endpoint"


@pytest.mark.asyncio
async def test_stale_investigation_sweep() -> None:
    """Verify sweep_stale transitions inactive running investigations to failed."""
    repo = InMemoryInvestigationRepository()
    service = InvestigationService(repo)

    resp, _ = await service.start(_sample_create_request())
    await service.transition_status(resp.id, InvestigationStatus.PROVISIONING)
    await service.transition_status(resp.id, InvestigationStatus.RUNNING)

    # Fake silence: last heartbeat 120s ago
    past = datetime.now(timezone.utc) - timedelta(seconds=120)
    await repo.update_heartbeat(resp.id, past)

    reconciled = await service.sweep_stale(silence_seconds=90)
    assert resp.id in reconciled

    detail = await service.get_by_id(resp.id)
    assert detail.status == InvestigationStatus.FAILED
    assert "heartbeat silence" in (detail.error or "")
