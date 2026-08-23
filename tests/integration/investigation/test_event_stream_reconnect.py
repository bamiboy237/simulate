"""Integration test for Server-Sent Events stream persistence and reconnects."""

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from app.config import Settings
from app.db import get_session_factory
from app.domain.investigation.repository import SqlAlchemyInvestigationRepository
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


def _database_url_or_skip() -> Settings:
    try:
        settings = Settings()  # type: ignore[call-arg]
    except ValidationError:
        pytest.skip("DATABASE_URL is required for database integration tests")
    return settings


@pytest.mark.integration
@pytest.mark.asyncio
async def test_event_stream_reconnect_replays_ordered_without_duplicates() -> None:
    """Verify disconnecting and reconnecting with Last-Event-ID replays all missing events."""
    _database_url_or_skip()

    session_factory = get_session_factory()
    async with session_factory() as session:
        repo = SqlAlchemyInvestigationRepository(session)
        service = InvestigationService(repo)

        create_req = InvestigationCreateRequest(
            trace_ref={"trace_id": "tr_reconnect_test"},
            world_ref={"world_id": "support_v1"},
            slice_ref=EnvironmentSliceRef(
                world_id="support_v1",
                world_version="1.0",
                slice_name="reconnect_slice",
                fixture_bundle_ref="bundle_recon",
                provenance="observed",
            ),
            investigator_ref=AgentArtifactRef(
                kind="prime_profile",
                digest_or_profile="prime@0.2.0",
            ),
        )

        resp, token = await service.start(create_req)
        await service.transition_status(resp.id, InvestigationStatus.PROVISIONING)
        await service.transition_status(resp.id, InvestigationStatus.RUNNING)
        await session.commit()

        # 1. Insert initial events 1, 2, 3
        now = datetime.now(timezone.utc)
        events_batch1 = [
            RunnerEvent(
                investigation_id=resp.id,
                seq=1,
                type=EventType.investigation_started,
                payload={"info": "started"},
                emitted_at=now,
            ),
            RunnerEvent(
                investigation_id=resp.id,
                seq=2,
                type=EventType.agent_ready,
                payload={},
                emitted_at=now,
            ),
            RunnerEvent(
                investigation_id=resp.id,
                seq=3,
                type=EventType.thought,
                payload={"thought": "inspecting records"},
                emitted_at=now,
            ),
        ]
        await service.record_events(token, events_batch1)
        await session.commit()

        # 2. Query initial events through service stream reader
        first_pull = await service.get_events_stream(resp.id, after_seq=0)
        assert [e.seq for e in first_pull] == [1, 2, 3]

        # 3. Simulate client was disconnected having seen up to seq=2
        # More events 4, 5 arrive while client is disconnected
        events_batch2 = [
            RunnerEvent(
                investigation_id=resp.id,
                seq=4,
                type=EventType.tool_call,
                payload={"tool": "evidence.read"},
                emitted_at=now,
            ),
            RunnerEvent(
                investigation_id=resp.id,
                seq=5,
                type=EventType.summary_submitted,
                payload={"findings": "Bug found", "next_step": "Fix patch"},
                emitted_at=now,
            ),
        ]
        await service.record_events(token, events_batch2)
        await session.commit()

        # 4. Reconnect with Last-Event-ID=2: fetch missing events
        reconnect_pull = await service.get_events_stream(resp.id, after_seq=2)
        assert [e.seq for e in reconnect_pull] == [3, 4, 5]
        assert reconnect_pull[0].type == "thought"
        assert reconnect_pull[1].type == "tool_call"
        assert reconnect_pull[2].type == "summary_submitted"
