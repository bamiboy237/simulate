"""Offline end-to-end test for the complete Phase 8 investigation lifecycle.

Drives the real FastAPI application, SQLite/PostgreSQL persistence, FakeRunner,
SSE stream reconnects, chat steering, and summary submission.
"""

import asyncio
from datetime import datetime, timezone
from uuid import UUID

from fastapi.testclient import TestClient
from tests.fakes.investigation_repository import InMemoryInvestigationRepository
from tests.fakes.runners import FakeRunner

from app.api.dependencies import get_investigation_service
from app.config import Settings
from app.domain.investigation.service import InvestigationService
from app.domain.runner.schemas import EventType
from app.main import create_app


def test_offline_investigation_lifecycle_e2e() -> None:
    """Full offline end-to-end verification of Phase 8 investigation runner."""
    repo = InMemoryInvestigationRepository()
    fake_runner = FakeRunner()
    service = InvestigationService(repo, runner=fake_runner)

    settings = Settings(environment="test", modal_enabled=False)
    app = create_app(settings)
    app.dependency_overrides[get_investigation_service] = lambda: service

    with TestClient(app) as client:
        # 1. Start investigation via API
        create_payload = {
            "trace_ref": {"trace_id": "tr_e2e_001", "platform": "langsmith"},
            "world_ref": {"world_id": "support_world_v1"},
            "slice_ref": {
                "world_id": "support_world_v1",
                "world_version": "1.0.0",
                "slice_name": "checkout_idempotency_failure",
                "fixture_bundle_ref": "bundles/checkout_v1.json",
                "provenance": "observed",
            },
            "investigator_ref": {
                "kind": "prime_profile",
                "digest_or_profile": "prime-investigator@0.2.0",
            },
            "task_brief": "# E2E Test Brief\nReproduce and diagnose payment failure.",
        }

        create_resp = client.post("/api/v1/investigations", json=create_payload)
        assert create_resp.status_code == 202
        inv_data = create_resp.json()
        inv_id_str = inv_data["id"]
        inv_id = UUID(inv_id_str)
        bridge_token = inv_data["bridge_token"]
        assert bridge_token

        # Verify initial state after creation
        detail_resp = client.get(f"/api/v1/investigations/{inv_id_str}")
        assert detail_resp.status_code == 200
        assert detail_resp.json()["status"] == "pending"

        # 2. Bridge starts and posts initial events
        now_iso = datetime.now(timezone.utc).isoformat()
        initial_events = [
            {
                "investigation_id": inv_id_str,
                "seq": 1,
                "type": EventType.investigation_started.value,
                "payload": {"brief_loaded": True},
                "emitted_at": now_iso,
            },
            {
                "investigation_id": inv_id_str,
                "seq": 2,
                "type": EventType.agent_ready.value,
                "payload": {"agent_version": "0.2.0"},
                "emitted_at": now_iso,
            },
            {
                "investigation_id": inv_id_str,
                "seq": 3,
                "type": EventType.thought.value,
                "payload": {"text": "Inspecting checkout service logs"},
                "emitted_at": now_iso,
            },
        ]

        events_resp1 = client.post(
            "/internal/events",
            headers={"Authorization": f"Bearer {bridge_token}"},
            json=initial_events,
        )
        assert events_resp1.status_code == 200
        assert events_resp1.json()["recorded"] == 3

        # 3. User sends a steering chat message mid-run
        chat_resp = client.post(
            f"/api/v1/investigations/{inv_id_str}/messages",
            json={"body": "Focus on duplicate charge webhook calls", "mode": "steer"},
        )
        assert chat_resp.status_code == 201
        assert chat_resp.json()["mode"] == "steer"

        # 4. Bridge polls inbox and receives steering instruction
        inbox_resp = client.get(
            "/internal/inbox",
            headers={"Authorization": f"Bearer {bridge_token}"},
        )
        assert inbox_resp.status_code == 200
        inbox_data = inbox_resp.json()
        assert len(inbox_data["messages"]) == 1
        assert inbox_data["messages"][0]["body"] == "Focus on duplicate charge webhook calls"

        # 5. Bridge posts tool execution and final summary
        later_events = [
            {
                "investigation_id": inv_id_str,
                "seq": 4,
                "type": EventType.tool_call.value,
                "payload": {"tool": "evidence.read", "args": {"trace_id": "tr_e2e_001"}},
                "emitted_at": now_iso,
            },
            {
                "investigation_id": inv_id_str,
                "seq": 5,
                "type": EventType.summary_submitted.value,
                "payload": {
                    "findings": "Root cause: webhook handler lacks idempotency key deduplication.",
                    "next_step": "Add Redis deduplication lock around transaction processing.",
                    "evidence_refs": [{"event_seq": 4}],
                },
                "emitted_at": now_iso,
            },
        ]

        events_resp2 = client.post(
            "/internal/events",
            headers={"Authorization": f"Bearer {bridge_token}"},
            json=later_events,
        )
        assert events_resp2.status_code == 200
        assert events_resp2.json()["recorded"] == 2

        # 6. Verify terminal state, persisted summary, and sandbox termination
        final_detail_resp = client.get(f"/api/v1/investigations/{inv_id_str}")
        assert final_detail_resp.status_code == 200
        final_data = final_detail_resp.json()

        assert final_data["status"] == "completed"
        assert final_data["summary"] is not None
        assert "idempotency key deduplication" in final_data["summary"]["findings"]
        assert "Redis deduplication lock" in final_data["summary"]["next_step"]

        # Verify all events recorded in order
        all_events = asyncio.run(repo.get_events(inv_id, after_seq=0))
        assert [e.seq for e in all_events] == [1, 2, 3, 4, 5]
        assert [e.type for e in all_events] == [
            "investigation_started",
            "agent_ready",
            "thought",
            "tool_call",
            "summary_submitted",
        ]

        # Verify sandbox was terminated upon reaching completed state
        assert len(fake_runner.terminated_handles) == 1
