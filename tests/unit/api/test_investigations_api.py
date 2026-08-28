"""Unit tests for investigation HTTP routes and bridge endpoints."""

from datetime import datetime, timezone

from fastapi.testclient import TestClient

from app.api.dependencies import get_investigation_service
from app.config import Settings
from app.domain.investigation.service import InvestigationService
from app.main import create_app
from tests.fakes.investigation_repository import InMemoryInvestigationRepository
from tests.fakes.runners import FakeRunner


def _make_client() -> tuple[TestClient, InvestigationService]:
    repo = InMemoryInvestigationRepository()
    service = InvestigationService(repo, runner=FakeRunner())
    settings = Settings(environment="test", modal_enabled=False)
    app = create_app(settings)
    app.dependency_overrides[get_investigation_service] = lambda: service
    return TestClient(app), service


def test_create_and_list_investigations() -> None:
    """Verify POST /api/v1/investigations creates a run and returns 202."""
    client, _ = _make_client()

    payload = {
        "trace_ref": {"trace_id": "tr_1"},
        "world_ref": {"world_id": "support_v1"},
        "slice_ref": {
            "world_id": "support_v1",
            "world_version": "1.0",
            "slice_name": "billing",
            "fixture_bundle_ref": "bundle_1",
            "provenance": "observed",
        },
        "investigator_ref": {
            "kind": "prime_profile",
            "digest_or_profile": "prime@0.2.0",
        },
        "task_brief": "# Investigate billing error",
    }

    # 1. Create
    resp = client.post("/api/v1/investigations", json=payload)
    assert resp.status_code == 202
    data = resp.json()
    assert data["status"] == "pending"
    assert "bridge_token" in data
    inv_id = data["id"]

    # 2. List
    list_resp = client.get("/api/v1/investigations")
    assert list_resp.status_code == 200
    list_data = list_resp.json()
    assert len(list_data) == 1
    assert list_data[0]["id"] == inv_id

    # 3. Detail
    detail_resp = client.get(f"/api/v1/investigations/{inv_id}")
    assert detail_resp.status_code == 200
    assert detail_resp.json()["id"] == inv_id


def test_chat_message_and_internal_inbox_flow() -> None:
    """Verify sending a user message and reading it via the bridge /internal/inbox."""
    client, service = _make_client()

    # Create investigation
    create_payload = {
        "trace_ref": {"trace_id": "tr_1"},
        "world_ref": {"world_id": "support_v1"},
        "slice_ref": {
            "world_id": "support_v1",
            "world_version": "1.0",
            "slice_name": "billing",
            "fixture_bundle_ref": "bundle_1",
            "provenance": "observed",
        },
        "investigator_ref": {
            "kind": "prime_profile",
            "digest_or_profile": "prime@0.2.0",
        },
    }
    create_resp = client.post("/api/v1/investigations", json=create_payload)
    inv_data = create_resp.json()
    inv_id = inv_data["id"]
    token = inv_data["bridge_token"]

    # Post user chat message
    msg_resp = client.post(
        f"/api/v1/investigations/{inv_id}/messages",
        json={"body": "Inspect customer accounts table", "mode": "steer"},
    )
    assert msg_resp.status_code == 201
    assert msg_resp.json()["body"] == "Inspect customer accounts table"

    # Bridge polls internal inbox with bearer auth
    inbox_resp = client.get(
        "/internal/inbox",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert inbox_resp.status_code == 200
    inbox_data = inbox_resp.json()
    assert len(inbox_data["messages"]) == 1
    assert inbox_data["messages"][0]["body"] == "Inspect customer accounts table"


def test_internal_events_ingestion() -> None:
    """Verify bridge can post events to /internal/events with token authentication."""
    client, _ = _make_client()

    create_payload = {
        "trace_ref": {"trace_id": "tr_1"},
        "world_ref": {"world_id": "support_v1"},
        "slice_ref": {
            "world_id": "support_v1",
            "world_version": "1.0",
            "slice_name": "billing",
            "fixture_bundle_ref": "bundle_1",
            "provenance": "observed",
        },
        "investigator_ref": {
            "kind": "prime_profile",
            "digest_or_profile": "prime@0.2.0",
        },
    }
    create_resp = client.post("/api/v1/investigations", json=create_payload)
    inv_data = create_resp.json()
    inv_id = inv_data["id"]
    token = inv_data["bridge_token"]

    # Unauthenticated request rejected
    unauth_resp = client.post(
        "/internal/events",
        json=[],
    )
    assert unauth_resp.status_code == 403 or unauth_resp.status_code == 401

    # Post events with bearer token
    now_iso = datetime.now(timezone.utc).isoformat()
    events_payload = [
        {
            "investigation_id": inv_id,
            "seq": 1,
            "type": "investigation_started",
            "payload": {"brief": "ready"},
            "emitted_at": now_iso,
        },
        {
            "investigation_id": inv_id,
            "seq": 2,
            "type": "thought",
            "payload": {"thought": "Querying SQL database"},
            "emitted_at": now_iso,
        },
    ]

    events_resp = client.post(
        "/internal/events",
        headers={"Authorization": f"Bearer {token}"},
        json=events_payload,
    )
    assert events_resp.status_code == 200
    assert events_resp.json()["ok"] is True
    assert events_resp.json()["recorded"] == 2
