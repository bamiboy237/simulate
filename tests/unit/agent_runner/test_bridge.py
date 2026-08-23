"""Unit tests for SandboxBridge event batching, steer downgrade, and inbox polling."""

from typing import Any
from uuid import uuid4

import httpx
import pytest

from app.domain.agent_runner.bridge import SandboxBridge


def test_steer_downgrade_rule() -> None:
    """Verify steer messages are downgraded to follow_up if no run is active."""
    inv_id = uuid4()
    bridge = SandboxBridge(
        control_plane_url="http://control-plane",
        bridge_token="tok_123",
        investigation_id=inv_id,
    )
    assert not bridge.is_run_active

    # 1. When inactive, steer becomes follow_up
    cmd1 = bridge.process_incoming_chat("Redirect focus", mode="steer")
    assert '"command":"follow_up"' in cmd1

    # 2. Prompt activates the run
    cmd2 = bridge.process_incoming_chat("Start task", mode="prompt")
    assert '"command":"prompt"' in cmd2
    assert bridge.is_run_active

    # 3. When active, steer remains steer
    cmd3 = bridge.process_incoming_chat("Check refund table", mode="steer")
    assert '"command":"steer"' in cmd3


@pytest.mark.asyncio
async def test_bridge_event_batching_and_flush() -> None:
    """Verify events are batched up to batch_size and sent to /internal/events."""
    inv_id = uuid4()
    received_batches: list[list[dict[str, Any]]] = []

    async def mock_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/internal/events":
            import json

            data = json.loads(request.content.decode())
            received_batches.append(data)
            return httpx.Response(200, json={"ok": True})
        return httpx.Response(404)

    transport = httpx.MockTransport(mock_handler)
    client = httpx.AsyncClient(transport=transport)

    bridge = SandboxBridge(
        control_plane_url="http://control-plane",
        bridge_token="tok_123",
        investigation_id=inv_id,
        http_client=client,
        batch_size=50,
    )

    # Buffer 49 thought lines - below batch limit
    for i in range(49):
        is_done = await bridge.handle_stdout_line(
            f'{{"type": "thought", "payload": {{"step": {i}}}}}',
            client,
        )
        assert not is_done
    assert len(received_batches) == 0

    # 50th line triggers immediate batch flush
    await bridge.handle_stdout_line('{"type": "thought", "payload": {"step": 50}}', client)
    assert len(received_batches) == 1
    assert len(received_batches[0]) == 50

    await client.aclose()


@pytest.mark.asyncio
async def test_bridge_summary_submitted_detection_and_exit() -> None:
    """Verify summary_submitted event triggers flush and signals completion."""
    inv_id = uuid4()
    received_batches: list[list[dict[str, Any]]] = []

    async def mock_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/internal/events":
            import json

            received_batches.append(json.loads(request.content.decode()))
            return httpx.Response(200, json={"ok": True})
        return httpx.Response(404)

    transport = httpx.MockTransport(mock_handler)
    client = httpx.AsyncClient(transport=transport)

    bridge = SandboxBridge(
        control_plane_url="http://control-plane",
        bridge_token="tok_123",
        investigation_id=inv_id,
        http_client=client,
    )

    summary_line = (
        '{"type": "summary_submitted", "payload": {"findings": "Root cause verified"}}'
    )
    is_done = await bridge.handle_stdout_line(summary_line, client)

    assert is_done is True
    assert len(received_batches) == 1
    assert received_batches[0][0]["type"] == "summary_submitted"

    await client.aclose()


@pytest.mark.asyncio
async def test_bridge_inbox_polling_and_backoff() -> None:
    """Verify inbox polling advances cursor and handles backoff."""
    inv_id = uuid4()
    call_count = 0

    async def mock_handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        if request.url.path == "/internal/inbox":
            if call_count == 1:
                return httpx.Response(
                    200,
                    json={
                        "messages": [{"id": "m1", "body": "test", "mode": "steer"}],
                        "next_cursor": 1,
                    },
                )
            return httpx.Response(500)
        return httpx.Response(404)

    transport = httpx.MockTransport(mock_handler)
    client = httpx.AsyncClient(transport=transport)

    bridge = SandboxBridge(
        control_plane_url="http://control-plane",
        bridge_token="tok_123",
        investigation_id=inv_id,
        http_client=client,
    )

    # First poll succeeds
    msgs = await bridge.poll_inbox(client)
    assert len(msgs) == 1
    assert bridge.cursor == 1
    assert bridge.backoff_s == 1.0

    # Second poll fails and backs off
    failed_msgs = await bridge.poll_inbox(client)
    assert len(failed_msgs) == 0
    assert bridge.backoff_s == 2.0

    await client.aclose()
