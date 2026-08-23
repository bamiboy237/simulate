"""Unit tests for World Gateway tool routes, authentication, and error handling."""

import pytest
from fastapi.testclient import TestClient

from app.domain.agent_runner.gateway import SUPPORTED_TOOLS, create_gateway_app


@pytest.fixture
def client() -> TestClient:
    app = create_gateway_app(
        bridge_token="secret-bridge-tok-123",
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
        resp = client.post(
            f"/tools/{tool}",
            json={"arguments": {"key": "value"}},
            headers=headers,
        )
        assert resp.status_code == 200, f"Tool {tool} failed with status {resp.status_code}"
        data = resp.json()
        assert data["tool"] == tool
        assert "result" in data
