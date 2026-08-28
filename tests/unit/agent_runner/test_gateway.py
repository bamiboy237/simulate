"""Unit tests for World Gateway tool routes, authentication, event emission, and validation."""

from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.domain.agent_runner.gateway import SUPPORTED_TOOLS, create_gateway_app
from app.domain.runner.schemas import EventType


@pytest.fixture
def client() -> TestClient:
    app = create_gateway_app(
        gateway_token="secret-bridge-tok-123",
        context={
            "world_id": "world-test",
            "slice_name": "slice-alpha",
            "trace": {"id": "tr-999"},
        },
    )
    return TestClient(app)


def test_gateway_auth_required(client: TestClient) -> None:
    """Verify unauthorized requests are rejected."""
    # No auth header -> 401
    resp = client.post("/tools/trace.read", json={})
    assert resp.status_code == 401

    # Invalid token -> 403
    resp_wrong = client.post(
        "/tools/trace.read",
        json={},
        headers={"Authorization": "Bearer wrong-token"},
    )
    assert resp_wrong.status_code == 403


def test_gateway_unknown_tool_returns_404(client: TestClient) -> None:
    """Verify unknown tool endpoints return 404."""
    resp = client.post(
        "/tools/unsupported.tool",
        json={},
        headers={"Authorization": "Bearer secret-bridge-tok-123"},
    )
    assert resp.status_code == 404


def test_gateway_all_supported_tools_happy_path(client: TestClient) -> None:
    """Verify all 9 supported skills return 200 with result payloads."""
    headers = {"Authorization": "Bearer secret-bridge-tok-123"}
    assert len(SUPPORTED_TOOLS) == 9

    for tool in SUPPORTED_TOOLS:
        args: dict[str, Any] = {"key": "value"}
        if tool == "summary.submit":
            args = {
                "findings": "Found root cause",
                "next_step": "Fix index",
                "evidence_refs": ["ev_1"],
            }
        elif tool == "scenario.run":
            args = {"scenario": "reproduce_crash"}
        elif tool == "proposal.create":
            args = {"title": "Fix plan", "hypothesis": "Missing null check"}

        resp = client.post(
            f"/tools/{tool}",
            json={"arguments": args},
            headers=headers,
        )
        assert resp.status_code == 200, f"Tool {tool} failed with status {resp.status_code}"
        data = resp.json()
        assert data["tool"] == tool
        assert "result" in data


def test_gateway_event_callback_and_summary_submission() -> None:
    """Verify event callback receives tool_call, tool_result, and exactly one summary_submitted."""
    events: list[tuple[EventType, dict[str, Any] | None]] = []

    def record_event(event_type: EventType, payload: dict[str, Any] | None) -> None:
        events.append((event_type, payload))

    app = create_gateway_app(
        gateway_token="secret-bridge-tok-123",
        context={"world_id": "w1"},
        event_callback=record_event,
    )
    client = TestClient(app)
    headers = {"Authorization": "Bearer secret-bridge-tok-123"}

    # 1. Non-mutating tool call
    resp = client.post(
        "/tools/trace.read",
        json={"arguments": {"trace_id": "tr_1"}},
        headers=headers,
    )
    assert resp.status_code == 200
    assert len(events) == 2
    assert events[0][0] == EventType.tool_call
    assert events[0][1]["tool"] == "trace.read"  # type: ignore
    assert events[1][0] == EventType.tool_result
    assert events[1][1]["tool"] == "trace.read"  # type: ignore
    assert app.state.service.has_mutated is False

    # 2. Mutating tool call (scenario.run)
    resp_scen = client.post(
        "/tools/scenario.run",
        json={"arguments": {"scenario": "s1"}},
        headers=headers,
    )
    assert resp_scen.status_code == 200
    assert app.state.service.has_mutated is True

    # 3. Malformed summary.submit (missing next_step)
    resp_bad_summary = client.post(
        "/tools/summary.submit",
        json={"arguments": {"findings": "Root cause discovered"}},  # missing next_step
        headers=headers,
    )
    assert resp_bad_summary.status_code == 400
    assert "findings" in resp_bad_summary.json()["detail"]
    # Verify no summary_submitted event was emitted
    summary_events = [e for e in events if e[0] == EventType.summary_submitted]
    assert len(summary_events) == 0

    # 4. Valid summary.submit
    resp_valid_summary = client.post(
        "/tools/summary.submit",
        json={
            "arguments": {
                "findings": "Bug in refund calculation logic",
                "next_step": "Deploy fix to service",
                "evidence_refs": ["ev_101", "ev_102"],
            }
        },
        headers=headers,
    )
    assert resp_valid_summary.status_code == 200
    summary_events = [e for e in events if e[0] == EventType.summary_submitted]
    assert len(summary_events) == 1
    assert summary_events[0][1]["findings"] == "Bug in refund calculation logic"  # type: ignore
    assert summary_events[0][1]["next_step"] == "Deploy fix to service"  # type: ignore
    assert summary_events[0][1]["evidence_refs"] == ["ev_101", "ev_102"]  # type: ignore

    # 5. Duplicate summary.submit emits tool events but never a second summary event.
    resp_dup = client.post(
        "/tools/summary.submit",
        json={
            "arguments": {
                "findings": "Updated final findings",
                "next_step": "Rollback and redeploy",
                "evidence_refs": ["ev_101", "ev_202"],
            }
        },
        headers=headers,
    )
    assert resp_dup.status_code == 200
    summary_events = [e for e in events if e[0] == EventType.summary_submitted]
    assert len(summary_events) == 1
    # The recorded summary reflects the latest accepted payload.
    assert app.state.service.submitted_summary["next_step"] == "Rollback and redeploy"  # type: ignore


def test_gateway_state_evidence_diff_trace_unavailable_without_compiled_data(
    client: TestClient,
) -> None:
    """Verify uncompiled state/evidence/diff/trace return explicit unavailable."""
    headers = {"Authorization": "Bearer secret-bridge-tok-123"}
    for tool in ("state.inspect", "state.diff", "evidence.read", "trace.read"):
        resp = client.post(
            f"/tools/{tool}",
            json={"arguments": {}},
            headers=headers,
        )
        assert resp.status_code == 200
        result = resp.json()["result"]
        assert result["available"] is False
        assert isinstance(result["reason"], str)
        assert result["reason"].strip()


def test_gateway_does_not_invent_world_or_trace_defaults(client: TestClient) -> None:
    """Verify World Gateway never fabricates world/slice/trace defaults."""
    headers = {"Authorization": "Bearer secret-bridge-tok-123"}

    world = client.post("/tools/world.describe", json={}, headers=headers).json()["result"]
    assert "world_default" not in world["world_id"]
    assert "slice_default" not in world["slice_name"]

    trace = client.post("/tools/trace.read", json={}, headers=headers).json()["result"]
    assert trace["available"] is False
    assert "trace_default" not in str(trace)


def test_gateway_returns_compiled_context_state_when_present() -> None:
    """Verify explicitly provided context state is served truthfully."""
    app = create_gateway_app(
        gateway_token="tok-true-state",
        context={"state": {"orders": [{"id": 1, "status": "pending"}]}},
    )
    gw_client = TestClient(app)
    resp = gw_client.post(
        "/tools/state.inspect",
        json={"arguments": {}},
        headers={"Authorization": "Bearer tok-true-state"},
    )
    assert resp.status_code == 200
    assert resp.json()["result"]["state"] == {"orders": [{"id": 1, "status": "pending"}]}


def test_gateway_trace_ref_served_as_metadata_only() -> None:
    """Verify a provided trace reference is returned as metadata, not fake data."""
    app = create_gateway_app(
        gateway_token="tok-trace-ref",
        context={"trace": {"trace_id": "tr-123", "source": "langsmith"}},
    )
    gw_client = TestClient(app)
    resp = gw_client.post(
        "/tools/trace.read",
        json={"arguments": {}},
        headers={"Authorization": "Bearer tok-trace-ref"},
    )
    assert resp.status_code == 200
    result = resp.json()["result"]
    assert result["available"] is False
    assert result["trace_ref"] == {"trace_id": "tr-123", "source": "langsmith"}


def test_gateway_environment_status_does_not_claim_fabricated_database() -> None:
    """Verify environment.status reports only what the sandbox can prove."""
    app = create_gateway_app(gateway_token="tok-env", context={"investigation_id": "inv-1"})
    gw_client = TestClient(app)
    resp = gw_client.post(
        "/tools/environment.status",
        json={"arguments": {}},
        headers={"Authorization": "Bearer tok-env"},
    )
    assert resp.status_code == 200
    result = resp.json()["result"]
    assert result["status"] == "healthy"
    assert "database" not in result


def test_gateway_summary_requires_evidence_refs_list(client: TestClient) -> None:
    """Verify summary.submit rejects missing or non-list evidence_refs."""
    headers = {"Authorization": "Bearer secret-bridge-tok-123"}

    # Missing evidence_refs entirely
    resp_missing = client.post(
        "/tools/summary.submit",
        json={"arguments": {"findings": "F", "next_step": "N"}},
        headers=headers,
    )
    assert resp_missing.status_code == 400
    assert "evidence_refs" in resp_missing.json()["detail"]

    # evidence_refs provided but not a list
    resp_wrong_type = client.post(
        "/tools/summary.submit",
        json={
            "arguments": {
                "findings": "F",
                "next_step": "N",
                "evidence_refs": "ev_1",
            }
        },
        headers=headers,
    )
    assert resp_wrong_type.status_code == 400
    assert "evidence_refs" in resp_wrong_type.json()["detail"]

    # Blank findings are rejected
    resp_blank = client.post(
        "/tools/summary.submit",
        json={
            "arguments": {
                "findings": "   ",
                "next_step": "N",
                "evidence_refs": ["ev_1"],
            }
        },
        headers=headers,
    )
    assert resp_blank.status_code == 400
    assert "findings" in resp_blank.json()["detail"]

    # A valid empty evidence list still satisfies the "must be a list" rule.
    resp_empty_list = client.post(
        "/tools/summary.submit",
        json={
            "arguments": {
                "findings": "F",
                "next_step": "N",
                "evidence_refs": [],
            }
        },
        headers=headers,
    )
    assert resp_empty_list.status_code == 200
