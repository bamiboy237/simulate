"""Unit tests for SandboxBridge lifecycle, supervisor loops, retries, and cleanup."""

import asyncio
import json
import socket
from collections.abc import Callable
from typing import Any
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient

from app.domain.agent_runner.bridge import SandboxBridge, main
from app.domain.agent_runner.gateway import create_gateway_app
from app.domain.runner.schemas import EventType


class FakeStreamWriter:
    def __init__(
        self,
        buffer: list[str],
        on_write: Callable[[str, "FakeStreamReader"], None] | None = None,
        reader: "FakeStreamReader | None" = None,
    ) -> None:
        self.buffer = buffer
        self._on_write = on_write
        self._reader = reader
        self._closing = False

    def write(self, data: bytes) -> None:
        text = data.decode("utf-8")
        self.buffer.append(text)
        if self._on_write is not None and self._reader is not None:
            self._on_write(text, self._reader)

    async def drain(self) -> None:
        pass

    def close(self) -> None:
        self._closing = True

    def is_closing(self) -> bool:
        return self._closing


class FakeStreamReader:
    def __init__(
        self,
        lines: list[str],
        delay_s: float = 0.0,
        stays_open: bool = False,
    ) -> None:
        self.lines = list(lines)
        self.delay_s = delay_s
        self.index = 0
        self.stays_open = stays_open

    def at_eof(self) -> bool:
        if self.stays_open and self.index >= len(self.lines):
            # The pipe is still open: EOF is not reached even though the
            # scripted lines are exhausted.
            return False
        return self.index >= len(self.lines)

    def append_line(self, line: str) -> None:
        """Inject a new stdout line, e.g. when a test fake reacts to an abort."""
        self.lines.append(line)

    async def readline(self) -> bytes:
        if self.at_eof():
            return b""
        # Wait briefly for injected lines (for example, lines the fake emits in
        # response to an abort) while keeping the pipe logically open.
        while self.index >= len(self.lines):
            await asyncio.sleep(0.005)
        line = self.lines[self.index]
        self.index += 1
        if self.delay_s > 0:
            await asyncio.sleep(self.delay_s)
        if not line.endswith("\n"):
            line += "\n"
        return line.encode("utf-8")


class FakeRpcProcess:
    def __init__(
        self,
        stdout_lines: list[str] | None = None,
        stderr_lines: list[str] | None = None,
        returncode: int = 0,
        delay_s: float = 0.0,
        stdout_stays_open: bool = False,
        on_stdin: Callable[[str, "FakeStreamReader"], None] | None = None,
    ) -> None:
        self.received_stdin: list[str] = []
        self._stdout_lines = list(stdout_lines or [])
        self._stderr_lines = list(stderr_lines or [])
        self.returncode = returncode
        self.delay_s = delay_s
        self.terminated = False

        self.stdout = FakeStreamReader(
            self._stdout_lines,
            delay_s=self.delay_s,
            stays_open=stdout_stays_open,
        )
        self.stderr = FakeStreamReader(self._stderr_lines)
        self.stdin = FakeStreamWriter(self.received_stdin, on_write=on_stdin, reader=self.stdout)

    async def wait(self) -> int:
        if self.delay_s > 0:
            await asyncio.sleep(self.delay_s)
        return self.returncode

    def terminate(self) -> None:
        self.terminated = True

    def kill(self) -> None:
        self.terminated = True


@pytest.mark.asyncio
async def test_rpc_process_never_disables_builtin_tools(monkeypatch: pytest.MonkeyPatch) -> None:
    """Prime Agent keeps IPython and extension tools: no --no-builtin-tools flag.

    The rejected diagnostic approach asserted a `--no-builtin-tools` launch flag.
    The correct repair keeps the built-in tool surface (IPython stays enabled)
    and instead bounds authority and runtime through the World Gateway token
    separation and the sanitized child environment.
    """
    captured: dict[str, Any] = {}

    async def fake_create_subprocess_exec(*args: str, **kwargs: Any) -> FakeRpcProcess:
        captured["args"] = args
        captured["kwargs"] = kwargs
        return FakeRpcProcess()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)
    bridge = SandboxBridge(
        control_plane_url="http://control-plane",
        bridge_token="tok_123",
        investigation_id=uuid4(),
    )
    bridge._explicit_child_env = {"PATH": "/usr/bin", "WORLD_GATEWAY_TOKEN": "gw_tok"}

    await bridge.start_rpc_process()

    assert "--no-builtin-tools" not in captured["args"]
    assert tuple(captured["args"]) == ("prime-agent", "--mode", "rpc", "--no-session")
    env = captured["kwargs"].get("env")
    assert env is not None
    # The only Prime-visible token is the World Gateway token.
    assert env.get("WORLD_GATEWAY_TOKEN") == "gw_tok"
    assert "BRIDGE_TOKEN" not in env


def test_world_gateway_token_is_separate_from_bridge_token() -> None:
    """Verify the per-run gateway token differs from the control-plane token."""
    bridge = SandboxBridge(
        control_plane_url="http://control-plane",
        bridge_token="tok_bridge_abc",
        investigation_id=uuid4(),
    )
    assert bridge.world_gateway_token
    assert bridge.world_gateway_token != "tok_bridge_abc"
    # Control-plane calls still authenticate with the bridge token.
    assert bridge.auth_headers["Authorization"] == "Bearer tok_bridge_abc"
    # The generated gateway token is never the bridge token in plain form.
    assert "tok_bridge_abc" != bridge.world_gateway_token


def test_build_child_env_sanitizes_credential_boundaries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify the Prime child env is a deliberate allowlist.

    BRIDGE_TOKEN, CONTROL_PLANE_CALLBACK_URL, task/control-plane data, database
    credentials, and unrelated secrets never reach Prime Agent. Model-provider
    credentials and World Gateway wiring do.
    """
    bridge = SandboxBridge(
        control_plane_url="http://control-plane",
        bridge_token="tok_bridge_secret",
        investigation_id=uuid4(),
        world_gateway_token="tok_gw_loopback",
    )
    # Simulate the container environment.
    monkeypatch.setenv("PATH", "/usr/local/bin:/usr/bin")
    monkeypatch.setenv("HOME", "/root")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-provider-secret")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-anthropic-secret")
    monkeypatch.setenv("BRIDGE_TOKEN", "tok_bridge_leaked")
    monkeypatch.setenv("CONTROL_PLANE_CALLBACK_URL", "https://control.example")
    monkeypatch.setenv("DATABASE_URL", "postgresql://user:pass@db:5432/db")
    monkeypatch.setenv("TASK_BRIEF", "super secret task content")
    monkeypatch.setenv("LANGSMITH_API_KEY", "ls-telemetry-key")
    monkeypatch.setenv("MODAL_SECRET_TOKEN", "modal-unrelated-secret")

    env = bridge.build_child_env()

    # Runtime basics and World Gateway wiring survive.
    assert env["PATH"] == "/usr/local/bin:/usr/bin"
    assert env["HOME"] == "/root"
    assert env["WORLD_GATEWAY_URL"] == "http://127.0.0.1:8001"
    assert env["WORLD_GATEWAY_TOKEN"] == "tok_gw_loopback"
    # Configured model-provider access survives and is visible to Prime.
    assert env["OPENAI_API_KEY"] == "sk-provider-secret"
    assert env["ANTHROPIC_API_KEY"] == "sk-anthropic-secret"

    # Everything else is excluded, including unrelated secrets.
    for forbidden in (
        "BRIDGE_TOKEN",
        "CONTROL_PLANE_CALLBACK_URL",
        "DATABASE_URL",
        "TASK_BRIEF",
        "LANGSMITH_API_KEY",
        "MODAL_SECRET_TOKEN",
    ):
        assert forbidden not in env, f"{forbidden} leaked into the Prime child env"
    assert "tok_bridge_leaked" not in json.dumps(env)
    assert "tok_bridge_secret" not in json.dumps(env)


def test_gateway_accepts_world_gateway_token_only() -> None:
    """Verify the gateway authenticates with WORLD_GATEWAY_TOKEN, never BRIDGE_TOKEN."""
    from fastapi.testclient import TestClient

    from app.domain.agent_runner.gateway import create_gateway_app

    bridge = SandboxBridge(
        control_plane_url="http://control-plane",
        bridge_token="tok_bridge_only",
        investigation_id=uuid4(),
    )
    app = create_gateway_app(gateway_token=bridge.world_gateway_token)
    client = TestClient(app)

    ok = client.post(
        "/tools/world.describe",
        json={},
        headers={"Authorization": f"Bearer {bridge.world_gateway_token}"},
    )
    assert ok.status_code == 200

    denied = client.post(
        "/tools/world.describe",
        json={},
        headers={"Authorization": f"Bearer {bridge.bridge_token}"},
    )
    assert denied.status_code == 403


def test_steer_downgrade_rule() -> None:
    """Verify steer messages are downgraded to follow_up if no run is active."""
    inv_id = uuid4()
    bridge = SandboxBridge(
        control_plane_url="http://control-plane",
        bridge_token="tok_123",
        investigation_id=inv_id,
    )
    assert not bridge.is_run_active

    # 1. When inactive, steer becomes follow_up bridge-side, wire is typed follow_up
    mode1, wire1 = bridge.process_incoming_chat("Redirect focus", mode="steer")
    assert mode1 == "follow_up"
    data1 = json.loads(wire1.strip())
    assert data1["type"] == "follow_up"
    assert data1["message"] == "Redirect focus"
    assert "content" not in data1

    # 2. Prompt activates the run
    mode2, wire2 = bridge.process_incoming_chat("Start task", mode="prompt")
    assert mode2 == "prompt"
    assert bridge.is_run_active
    data2 = json.loads(wire2.strip())
    assert data2["type"] == "prompt"
    assert data2["message"] == "Start task"

    # 3. When active, steer remains steer bridge-side
    mode3, wire3 = bridge.process_incoming_chat("Check refund table", mode="steer")
    assert mode3 == "steer"
    data3 = json.loads(wire3.strip())
    assert data3["type"] == "steer"
    assert data3["message"] == "Check refund table"


@pytest.mark.asyncio
async def test_bridge_event_batching_and_flush() -> None:
    """Verify events are batched up to batch_size and sent to /internal/events."""
    inv_id = uuid4()
    received_batches: list[list[dict[str, Any]]] = []

    async def mock_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/internal/events":
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
        line = (
            '{"type": "message_update", "assistantMessageEvent": '
            f'{{"type": "thinking_delta", "delta": "{i}"}}}}'
        )
        is_done = await bridge.handle_stdout_line(line, client)
        assert not is_done
    assert len(received_batches) == 0

    # 50th line triggers immediate batch flush
    line50 = (
        '{"type": "message_update", "assistantMessageEvent": '
        '{"type": "thinking_delta", "delta": "50"}}'
    )
    await bridge.handle_stdout_line(line50, client)
    assert len(received_batches) == 1
    assert len(received_batches[0]) == 50

    await client.aclose()


@pytest.mark.asyncio
async def test_task_brief_is_first_prompt_and_inbox_reaches_stdin_in_order() -> None:
    """Verify task brief is sent as first prompt and inbox messages follow in order."""
    inv_id = uuid4()
    received_events: list[dict[str, Any]] = []

    async def mock_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/internal/events":
            batch = json.loads(request.content.decode())
            received_events.extend(batch)
            return httpx.Response(200, json={"ok": True})
        if request.url.path == "/internal/inbox":
            return httpx.Response(
                200,
                json={
                    "messages": [
                        {"id": "m1", "body": "Check billing logs", "mode": "steer"},
                        {"id": "m2", "body": "Did order succeed?", "mode": "follow_up"},
                    ],
                    "next_cursor": 2,
                },
            )
        return httpx.Response(404)

    transport = httpx.MockTransport(mock_handler)
    client = httpx.AsyncClient(transport=transport)

    msg_line = (
        '{"type": "message_update", "message": {"role": "assistant", "content": []}, '
        '"assistantMessageEvent": {"type": "text_delta", "delta": "Understood"}}'
    )
    stdout_lines = [
        '{"type": "agent_start"}',
        msg_line,
        '{"type": "agent_end", "messages": []}',
    ]
    fake_proc = FakeRpcProcess(stdout_lines=stdout_lines, returncode=0, delay_s=0.01)

    bridge = SandboxBridge(
        control_plane_url="http://control-plane",
        bridge_token="tok_test_secret",
        investigation_id=inv_id,
        task_brief="# Task brief: Reproduce checkout 500 error",
        process_factory=lambda: asyncio.sleep(0, result=fake_proc),
        start_gateway_server=False,
        http_client=client,
        inbox_poll_interval_s=0.01,
    )
    # Simulate valid summary submission via gateway
    bridge.summary_submitted = True

    exit_code = await bridge.run()
    assert exit_code == 0

    # Verify task brief prompt reached stdin first
    assert len(fake_proc.received_stdin) >= 1
    first_cmd = json.loads(fake_proc.received_stdin[0].strip())
    assert first_cmd["type"] == "prompt"
    assert first_cmd["message"] == "# Task brief: Reproduce checkout 500 error"

    await client.aclose()


@pytest.mark.asyncio
async def test_valid_summary_plus_agent_end_clean_completion() -> None:
    """Verify run succeeds (exit 0) when valid summary and agent_end occur."""
    inv_id = uuid4()
    received_events: list[dict[str, Any]] = []

    async def mock_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/internal/events":
            received_events.extend(json.loads(request.content.decode()))
            return httpx.Response(200, json={"ok": True})
        if request.url.path == "/internal/inbox":
            return httpx.Response(200, json={"messages": [], "next_cursor": 0})
        return httpx.Response(404)

    client = httpx.AsyncClient(transport=httpx.MockTransport(mock_handler))

    msg_line = (
        '{"type": "message_update", "message": {"role": "assistant", "content": []}, '
        '"assistantMessageEvent": {"type": "text_delta", "delta": "Analysis complete"}}'
    )
    stdout_lines = [
        '{"type": "agent_start"}',
        msg_line,
        '{"type": "agent_end", "messages": []}',
    ]
    fake_proc = FakeRpcProcess(stdout_lines=stdout_lines, returncode=0)

    bridge = SandboxBridge(
        control_plane_url="http://control-plane",
        bridge_token="tok_test_secret",
        investigation_id=inv_id,
        task_brief="Analyze trace",
        process_factory=lambda: asyncio.sleep(0, result=fake_proc),
        start_gateway_server=False,
        http_client=client,
    )

    # Trigger gateway summary submit
    gw_app = create_gateway_app("tok_test_secret", event_callback=bridge._on_gateway_event)
    gw_client = TestClient(gw_app)
    gw_client.post(
        "/tools/summary.submit",
        json={
            "arguments": {
                "findings": "Database deadlock identified",
                "next_step": "Add query timeout",
                "evidence_refs": ["ev_1"],
            }
        },
        headers={"Authorization": "Bearer tok_test_secret"},
    )
    assert bridge.summary_submitted is True

    exit_code = await bridge.run()
    assert exit_code == 0

    event_types = [e["type"] for e in received_events]
    assert "investigation_started" in event_types
    assert "tool_call" in event_types
    assert "tool_result" in event_types
    assert "summary_submitted" in event_types
    assert "investigation_finished" in event_types

    await client.aclose()


@pytest.mark.asyncio
async def test_agent_end_without_summary_fails_visibly() -> None:
    """Verify run fails visibly (exit 1) if agent_end arrives without summary.submit."""
    inv_id = uuid4()
    received_events: list[dict[str, Any]] = []

    async def mock_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/internal/events":
            received_events.extend(json.loads(request.content.decode()))
            return httpx.Response(200, json={"ok": True})
        if request.url.path == "/internal/inbox":
            return httpx.Response(200, json={"messages": [], "next_cursor": 0})
        return httpx.Response(404)

    client = httpx.AsyncClient(transport=httpx.MockTransport(mock_handler))

    stdout_lines = [
        '{"type": "agent_start"}',
        '{"type": "agent_end", "messages": []}',
    ]
    # Process exits without calling summary.submit
    fake_proc = FakeRpcProcess(stdout_lines=stdout_lines, returncode=0)

    bridge = SandboxBridge(
        control_plane_url="http://control-plane",
        bridge_token="tok_test_secret",
        investigation_id=inv_id,
        task_brief="Analyze trace",
        process_factory=lambda: asyncio.sleep(0, result=fake_proc),
        start_gateway_server=False,
        http_client=client,
        max_retries=1,
    )

    exit_code = await bridge.run()
    assert exit_code == 1

    event_types = [e["type"] for e in received_events]
    assert "error" in event_types
    error_event = [e for e in received_events if e["type"] == "error"][0]
    assert "fatal" in error_event["payload"]

    await client.aclose()


@pytest.mark.asyncio
async def test_retry_before_mutation_vs_no_replay_after_mutation() -> None:
    """Verify crashes before mutation retry, but crashes after mutation abort immediately."""
    inv_id = uuid4()
    received_events: list[dict[str, Any]] = []

    async def mock_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/internal/events":
            received_events.extend(json.loads(request.content.decode()))
            return httpx.Response(200, json={"ok": True})
        if request.url.path == "/internal/inbox":
            return httpx.Response(200, json={"messages": [], "next_cursor": 0})
        return httpx.Response(404)

    client = httpx.AsyncClient(transport=httpx.MockTransport(mock_handler))

    spawn_count = 0

    async def proc_factory() -> FakeRpcProcess:
        nonlocal spawn_count
        spawn_count += 1
        if spawn_count == 1:
            # Crash before mutation
            return FakeRpcProcess(
                stdout_lines=['{"type": "agent_start"}'],
                stderr_lines=["Transient runtime bootstrap fault"],
                returncode=1,
            )
        else:
            # Second try: calls summary and finishes cleanly
            return FakeRpcProcess(
                stdout_lines=['{"type": "agent_start"}', '{"type": "agent_end", "messages": []}'],
                returncode=0,
            )

    bridge = SandboxBridge(
        control_plane_url="http://control-plane",
        bridge_token="tok_test_secret",
        investigation_id=inv_id,
        task_brief="Test retries",
        process_factory=proc_factory,
        start_gateway_server=False,
        http_client=client,
        max_retries=2,
    )
    bridge.summary_submitted = True

    exit_code = await bridge.run()
    assert exit_code == 0
    assert spawn_count == 2  # Proves retry occurred

    # Now test crash AFTER mutation: should NOT retry
    spawn_count_mut = 0

    async def proc_factory_mut() -> FakeRpcProcess:
        nonlocal spawn_count_mut
        spawn_count_mut += 1
        return FakeRpcProcess(
            stdout_lines=['{"type": "agent_start"}'],
            stderr_lines=["Crash after state change"],
            returncode=137,
        )

    bridge_mut = SandboxBridge(
        control_plane_url="http://control-plane",
        bridge_token="tok_test_secret",
        investigation_id=inv_id,
        task_brief="Test no replay after mutation",
        process_factory=proc_factory_mut,
        start_gateway_server=False,
        http_client=client,
        max_retries=3,
    )
    # Simulate a mutation occurred via scenario.run
    bridge_mut.has_mutated = True

    exit_code_mut = await bridge_mut.run()
    assert exit_code_mut == 1
    assert spawn_count_mut == 1  # Did NOT retry after mutation

    await client.aclose()


@pytest.mark.asyncio
async def test_secrets_redaction_in_stderr_tail_and_cleanup() -> None:
    """Verify stderr diagnostic tail redacts secret bridge token on crash."""
    inv_id = uuid4()
    received_events: list[dict[str, Any]] = []

    async def mock_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/internal/events":
            received_events.extend(json.loads(request.content.decode()))
            return httpx.Response(200, json={"ok": True})
        if request.url.path == "/internal/inbox":
            return httpx.Response(200, json={"messages": [], "next_cursor": 0})
        return httpx.Response(404)

    client = httpx.AsyncClient(transport=httpx.MockTransport(mock_handler))

    secret_token = "tok_super_secret_bridge_9999"
    fake_proc = FakeRpcProcess(
        stdout_lines=[],
        stderr_lines=[
            f"Failed to connect to gateway with Authorization: Bearer {secret_token}",
            "Fatal internal process abortion",
        ],
        returncode=1,
    )

    bridge = SandboxBridge(
        control_plane_url="http://control-plane",
        bridge_token=secret_token,
        investigation_id=inv_id,
        task_brief="Test redaction",
        process_factory=lambda: asyncio.sleep(0, result=fake_proc),
        start_gateway_server=False,
        http_client=client,
        max_retries=0,
    )

    exit_code = await bridge.run()
    assert exit_code == 1

    # Verify secret is never leaked in error events or string dumps
    error_events = [e for e in received_events if e["type"] == "error"]
    assert len(error_events) >= 1
    err_str = json.dumps(error_events)
    assert secret_token not in err_str
    assert "[REDACTED]" in err_str

    await client.aclose()


@pytest.mark.asyncio
async def test_heartbeats_and_timed_flushes_below_batch_limit() -> None:
    """Verify heartbeats and timed event flushes occur without waiting for 50 events."""
    inv_id = uuid4()
    received_batches: list[list[dict[str, Any]]] = []

    async def mock_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/internal/events":
            received_batches.append(json.loads(request.content.decode()))
            return httpx.Response(200, json={"ok": True})
        if request.url.path == "/internal/inbox":
            return httpx.Response(200, json={"messages": [], "next_cursor": 0})
        return httpx.Response(404)

    client = httpx.AsyncClient(transport=httpx.MockTransport(mock_handler))

    # Long running process that outputs slowly
    msg_line = (
        '{"type": "message_update", "message": {"role": "assistant", "content": []}, '
        '"assistantMessageEvent": {"type": "thinking_delta", "delta": "Thinking step 1"}}'
    )
    stdout_lines = [
        '{"type": "agent_start"}',
        msg_line,
        '{"type": "agent_end", "messages": []}',
    ]
    fake_proc = FakeRpcProcess(stdout_lines=stdout_lines, returncode=0, delay_s=0.05)

    bridge = SandboxBridge(
        control_plane_url="http://control-plane",
        bridge_token="tok_test_secret",
        investigation_id=inv_id,
        task_brief="Heartbeat test",
        process_factory=lambda: asyncio.sleep(0, result=fake_proc),
        start_gateway_server=False,
        http_client=client,
        batch_size=50,
        batch_interval_s=0.02,
        heartbeat_interval_s=0.02,
    )
    bridge.summary_submitted = True

    exit_code = await bridge.run()
    assert exit_code == 0

    # Verify multiple smaller batches were sent (timed flush below 50 limit)
    all_events = [event for batch in received_batches for event in batch]
    event_types = [e["type"] for e in all_events]
    assert "heartbeat" in event_types
    assert "investigation_started" in event_types
    assert "investigation_finished" in event_types

    await client.aclose()


@pytest.mark.asyncio
async def test_summary_without_agent_end_does_not_report_clean_completion() -> None:
    """Verify summary.submit alone does not complete if process exits nonzero without agent_end."""
    inv_id = uuid4()
    received_events: list[dict[str, Any]] = []

    async def mock_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/internal/events":
            received_events.extend(json.loads(request.content.decode()))
            return httpx.Response(200, json={"ok": True})
        if request.url.path == "/internal/inbox":
            return httpx.Response(200, json={"messages": [], "next_cursor": 0})
        return httpx.Response(404)

    client = httpx.AsyncClient(transport=httpx.MockTransport(mock_handler))

    # Process submitted summary via gateway, but crashed before emitting agent_end
    stdout_lines = ['{"type": "agent_start"}']
    fake_proc = FakeRpcProcess(
        stdout_lines=stdout_lines,
        stderr_lines=["Killed by signal 9"],
        returncode=137,
    )

    bridge = SandboxBridge(
        control_plane_url="http://control-plane",
        bridge_token="tok_test_secret",
        investigation_id=inv_id,
        task_brief="Analyze trace",
        process_factory=lambda: asyncio.sleep(0, result=fake_proc),
        start_gateway_server=False,
        http_client=client,
        max_retries=0,
    )
    # Summary was submitted, but state mutated and process crashed with 137
    bridge.summary_submitted = True
    bridge.has_mutated = True

    exit_code = await bridge.run()
    assert exit_code == 1

    event_types = [e["type"] for e in received_events]
    assert "error" in event_types

    await client.aclose()


@pytest.mark.asyncio
async def test_protocol_failure_non_json_stdout_fails_visibly() -> None:
    """Verify non-JSON RPC stdout is a protocol failure: error, nonzero, no retry."""
    inv_id = uuid4()
    received_events: list[dict[str, Any]] = []

    async def mock_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/internal/events":
            received_events.extend(json.loads(request.content.decode()))
            return httpx.Response(200, json={"ok": True})
        if request.url.path == "/internal/inbox":
            return httpx.Response(200, json={"messages": [], "next_cursor": 0})
        return httpx.Response(404)

    client = httpx.AsyncClient(transport=httpx.MockTransport(mock_handler))

    fake_proc = FakeRpcProcess(
        stdout_lines=['{"type": "agent_start"}', "this-is-not-json"],
        returncode=0,
    )
    bridge = SandboxBridge(
        control_plane_url="http://control-plane",
        bridge_token="tok_test_secret",
        investigation_id=inv_id,
        task_brief="Test protocol",
        process_factory=lambda: asyncio.sleep(0, result=fake_proc),
        start_gateway_server=False,
        http_client=client,
        max_retries=3,
    )

    exit_code = await bridge.run()
    assert exit_code == 1
    assert bridge.protocol_failure is True

    errors = [e for e in received_events if e["type"] == "error"]
    assert len(errors) >= 1
    assert any("protocol" in e["payload"]["error"] for e in errors)

    # Garbage must never be surfaced as a normal message event.
    msg_events = [e for e in received_events if e["type"] == "message"]
    assert not any("this-is-not-json" in str(e["payload"]) for e in msg_events)

    await client.aclose()


@pytest.mark.asyncio
async def test_summary_without_agent_end_clean_exit_zero_still_fails() -> None:
    """Verify summary.submit alone does not complete even when the process exits 0."""
    inv_id = uuid4()
    received_events: list[dict[str, Any]] = []

    async def mock_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/internal/events":
            received_events.extend(json.loads(request.content.decode()))
            return httpx.Response(200, json={"ok": True})
        if request.url.path == "/internal/inbox":
            return httpx.Response(200, json={"messages": [], "next_cursor": 0})
        return httpx.Response(404)

    client = httpx.AsyncClient(transport=httpx.MockTransport(mock_handler))

    # Process exits 0 but never emits agent_end after the summary was submitted.
    fake_proc = FakeRpcProcess(
        stdout_lines=['{"type": "agent_start"}'],
        returncode=0,
    )
    bridge = SandboxBridge(
        control_plane_url="http://control-plane",
        bridge_token="tok_test_secret",
        investigation_id=inv_id,
        task_brief="Analyze trace",
        process_factory=lambda: asyncio.sleep(0, result=fake_proc),
        start_gateway_server=False,
        http_client=client,
        max_retries=0,
    )
    bridge.summary_submitted = True
    bridge.has_mutated = True

    exit_code = await bridge.run()
    assert exit_code == 1

    event_types = [e["type"] for e in received_events]
    assert "error" in event_types

    await client.aclose()


@pytest.mark.asyncio
async def test_control_plane_flush_failure_fails_and_records_error() -> None:
    """Verify undeliverable events to the control plane fail the run visibly."""
    inv_id = uuid4()

    async def mock_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/internal/events":
            # Control plane rejects every batch.
            return httpx.Response(500, json={"error": "down"})
        if request.url.path == "/internal/inbox":
            return httpx.Response(200, json={"messages": [], "next_cursor": 0})
        return httpx.Response(404)

    client = httpx.AsyncClient(transport=httpx.MockTransport(mock_handler))

    fake_proc = FakeRpcProcess(
        stdout_lines=['{"type": "agent_start"}', '{"type": "agent_end", "messages": []}'],
        returncode=0,
    )
    bridge = SandboxBridge(
        control_plane_url="http://control-plane",
        bridge_token="tok_test_secret",
        investigation_id=inv_id,
        task_brief="Test control plane",
        process_factory=lambda: asyncio.sleep(0, result=fake_proc),
        start_gateway_server=False,
        http_client=client,
        max_retries=0,
    )
    bridge.summary_submitted = True

    exit_code = await bridge.run()
    assert exit_code == 1

    # The failure is visible in the bridge's buffered events.
    errors = [e for e in bridge.event_buffer if e.type == EventType.error]
    assert len(errors) >= 1
    assert errors[0].type == EventType.error
    assert errors[0].payload["fatal"] is True

    await client.aclose()


@pytest.mark.asyncio
async def test_gateway_start_failure_fails_visibly(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify a gateway startup failure records a visible error and returns nonzero."""
    inv_id = uuid4()
    received_events: list[dict[str, Any]] = []

    async def mock_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/internal/events":
            received_events.extend(json.loads(request.content.decode()))
            return httpx.Response(200, json={"ok": True})
        return httpx.Response(404)

    client = httpx.AsyncClient(transport=httpx.MockTransport(mock_handler))

    def boom(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("gateway port already in use")

    monkeypatch.setattr("app.domain.agent_runner.bridge.create_gateway_app", boom)
    bridge = SandboxBridge(
        control_plane_url="http://control-plane",
        bridge_token="tok_test_secret",
        investigation_id=inv_id,
        task_brief="Test gateway start",
        start_gateway_server=True,
        http_client=client,
    )

    exit_code = await bridge.run()
    assert exit_code == 1

    errors = [e for e in received_events if e["type"] == "error"]
    assert len(errors) >= 1
    assert "gateway port already in use" in errors[0]["payload"]["error"]

    await client.aclose()


@pytest.mark.asyncio
async def test_inbox_chat_modes_reach_stdin_in_order() -> None:
    """Verify prompt, follow_up (downgraded steer), and steer reach stdin in order."""
    inv_id = uuid4()
    received_events: list[dict[str, Any]] = []
    inbox_calls = 0

    async def mock_handler(request: httpx.Request) -> httpx.Response:
        nonlocal inbox_calls
        if request.url.path == "/internal/events":
            received_events.extend(json.loads(request.content.decode()))
            return httpx.Response(200, json={"ok": True})
        if request.url.path == "/internal/inbox":
            inbox_calls += 1
            if inbox_calls == 1:
                return httpx.Response(
                    200,
                    json={
                        "messages": [{"id": "m1", "body": "Steer before run", "mode": "steer"}],
                        "next_cursor": 1,
                    },
                )
            if inbox_calls == 2:
                # Wait until agent_start has activated the run, then deliver steer.
                for _ in range(200):
                    if bridge.is_run_active:
                        break
                    await asyncio.sleep(0.01)
                return httpx.Response(
                    200,
                    json={
                        "messages": [{"id": "m2", "body": "Steer after run", "mode": "steer"}],
                        "next_cursor": 2,
                    },
                )
            return httpx.Response(200, json={"messages": [], "next_cursor": 2})
        return httpx.Response(404)

    client = httpx.AsyncClient(transport=httpx.MockTransport(mock_handler))

    fake_proc = FakeRpcProcess(
        stdout_lines=[
            '{"type": "agent_start"}',
            '{"type": "agent_end", "messages": []}',
        ],
        returncode=0,
        delay_s=0.05,
    )
    bridge = SandboxBridge(
        control_plane_url="http://control-plane",
        bridge_token="tok_test_secret",
        investigation_id=inv_id,
        task_brief="Start investigation",
        process_factory=lambda: asyncio.sleep(0, result=fake_proc),
        start_gateway_server=False,
        http_client=client,
        inbox_poll_interval_s=0.01,
        max_retries=0,
    )
    bridge.summary_submitted = True

    exit_code = await bridge.run()
    assert exit_code == 0

    commands = [json.loads(cmd.strip()) for cmd in fake_proc.received_stdin]

    # Task brief is the first prompt sent.
    assert commands[0]["type"] == "prompt"
    assert commands[0]["message"] == "Start investigation"

    # Incoming sequence in order: steering-before-run (downgraded) then steering.
    incoming = [c for c in commands[1:] if c["message"].startswith("Steer")]
    assert [c["type"] for c in incoming] == ["follow_up", "steer"]

    await client.aclose()


@pytest.mark.asyncio
async def test_gateway_tool_events_during_run_and_exactly_once_summary() -> None:
    """Verify run() records ordered tool events and one summary event via gateway."""
    inv_id = uuid4()
    received_events: list[dict[str, Any]] = []

    async def mock_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/internal/events":
            received_events.extend(json.loads(request.content.decode()))
            return httpx.Response(200, json={"ok": True})
        if request.url.path == "/internal/inbox":
            return httpx.Response(200, json={"messages": [], "next_cursor": 0})
        return httpx.Response(404)

    client = httpx.AsyncClient(transport=httpx.MockTransport(mock_handler))

    gw_app = create_gateway_app("tok_gw_secret")
    fake_proc = FakeRpcProcess(
        stdout_lines=[
            '{"type": "agent_start"}',
            '{"type": "agent_end", "messages": []}',
        ],
        returncode=0,
        delay_s=0.25,
    )
    bridge = SandboxBridge(
        control_plane_url="http://control-plane",
        bridge_token="tok_gw_secret",
        investigation_id=inv_id,
        task_brief="Probe gateway",
        process_factory=lambda: asyncio.sleep(0, result=fake_proc),
        gateway_app=gw_app,
        start_gateway_server=False,
        http_client=client,
        max_retries=0,
    )

    async def drive_gateway() -> None:
        headers = {"Authorization": "Bearer tok_gw_secret"}
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=gw_app),
            base_url="http://gw",
        ) as gw:
            await asyncio.sleep(0.05)
            r1 = await gw.post(
                "/tools/trace.read",
                json={"arguments": {"trace_id": "t1"}},
                headers=headers,
            )
            assert r1.status_code == 200
            r2 = await gw.post(
                "/tools/scenario.run",
                json={"arguments": {"scenario": "s1"}},
                headers=headers,
            )
            assert r2.status_code == 200
            r3 = await gw.post(
                "/tools/summary.submit",
                json={
                    "arguments": {
                        "findings": "F1",
                        "next_step": "N1",
                        "evidence_refs": ["ev_1"],
                    }
                },
                headers=headers,
            )
            assert r3.status_code == 200
            # Duplicate summary call: must not duplicate summary_submitted.
            r4 = await gw.post(
                "/tools/summary.submit",
                json={
                    "arguments": {
                        "findings": "F2",
                        "next_step": "N2",
                        "evidence_refs": ["ev_1", "ev_2"],
                    }
                },
                headers=headers,
            )
            assert r4.status_code == 200

    driver = asyncio.create_task(drive_gateway())
    exit_code = await bridge.run()
    await driver
    assert exit_code == 0

    event_types = [e["type"] for e in received_events]
    assert "investigation_started" in event_types
    assert "investigation_finished" in event_types

    calls = [
        (e["type"], e["payload"].get("tool"))
        for e in received_events
        if e["type"] in ("tool_call", "tool_result")
    ]
    assert ("tool_call", "trace.read") in calls
    assert ("tool_result", "trace.read") in calls
    assert ("tool_call", "scenario.run") in calls
    assert ("tool_result", "scenario.run") in calls
    assert ("tool_call", "summary.submit") in calls
    assert ("tool_result", "summary.submit") in calls

    summaries = [e for e in received_events if e["type"] == "summary_submitted"]
    assert len(summaries) == 1
    # The single persisted event carries the first accepted summary payload.
    assert summaries[0]["payload"]["findings"] == "F1"

    await client.aclose()


@pytest.mark.asyncio
async def test_loopback_gateway_start_stop_during_run() -> None:
    """Verify the bridge starts/stop a real loopback gateway and serves tool calls."""
    inv_id = uuid4()
    received_events: list[dict[str, Any]] = []

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        loopback_port = sock.getsockname()[1]

    async def mock_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/internal/events":
            received_events.extend(json.loads(request.content.decode()))
            return httpx.Response(200, json={"ok": True})
        if request.url.path == "/internal/inbox":
            return httpx.Response(200, json={"messages": [], "next_cursor": 0})
        return httpx.Response(404)

    client = httpx.AsyncClient(transport=httpx.MockTransport(mock_handler))

    fake_proc = FakeRpcProcess(
        stdout_lines=[
            '{"type": "agent_start"}',
            '{"type": "agent_end", "messages": []}',
        ],
        returncode=0,
        delay_s=0.3,
    )
    bridge = SandboxBridge(
        control_plane_url="http://control-plane",
        bridge_token="tok_loopback",
        investigation_id=inv_id,
        task_brief="Loopback test",
        gateway_port=loopback_port,
        process_factory=lambda: asyncio.sleep(0, result=fake_proc),
        start_gateway_server=True,
        http_client=client,
        max_retries=0,
    )

    async def hit_gateway() -> None:
        base = f"http://127.0.0.1:{loopback_port}"
        async with httpx.AsyncClient(base_url=base) as gw:
            await asyncio.sleep(0.1)
            ok = await gw.post(
                "/tools/environment.status",
                json={"arguments": {}},
                headers={"Authorization": f"Bearer {bridge.world_gateway_token}"},
            )
            assert ok.status_code == 200
            wrong = await gw.post(
                "/tools/environment.status",
                json={"arguments": {}},
                headers={"Authorization": "Bearer wrong-token"},
            )
            assert wrong.status_code == 403
            missing = await gw.post(
                "/tools/nope.tool",
                json={"arguments": {}},
                headers={"Authorization": f"Bearer {bridge.world_gateway_token}"},
            )
            assert missing.status_code == 404
            summary = await gw.post(
                "/tools/summary.submit",
                json={
                    "arguments": {
                        "findings": "L",
                        "next_step": "N",
                        "evidence_refs": ["ev_loop"],
                    }
                },
                headers={"Authorization": f"Bearer {bridge.world_gateway_token}"},
            )
            assert summary.status_code == 200

    driver = asyncio.create_task(hit_gateway())
    exit_code = await bridge.run()
    await driver
    assert exit_code == 0

    # The gateway server was started and then stopped by the bridge.
    assert bridge._gateway_server is None
    assert bridge._gateway_task is None
    event_types = [e["type"] for e in received_events]
    assert "tool_call" in event_types
    assert "tool_result" in event_types
    summaries = [e for e in received_events if e["type"] == "summary_submitted"]
    assert len(summaries) == 1

    await client.aclose()


@pytest.mark.asyncio
async def test_retry_does_not_duplicate_persisted_events() -> None:
    """Verify buffered events are persisted exactly once across a retry."""
    inv_id = uuid4()
    received_batches: list[list[dict[str, Any]]] = []
    spawn_count = 0

    async def mock_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/internal/events":
            received_batches.append(json.loads(request.content.decode()))
            return httpx.Response(200, json={"ok": True})
        if request.url.path == "/internal/inbox":
            return httpx.Response(200, json={"messages": [], "next_cursor": 0})
        return httpx.Response(404)

    client = httpx.AsyncClient(transport=httpx.MockTransport(mock_handler))

    def thinking(i: int) -> str:
        return (
            '{"type": "message_update", "assistantMessageEvent": '
            f'{{"type": "thinking_delta", "delta": "{i}"}}}}'
        )

    async def proc_factory() -> FakeRpcProcess:
        nonlocal spawn_count
        spawn_count += 1
        if spawn_count == 1:
            # Crashing attempt emits 55 events (50 flushed, 5 buffered) then crashes.
            lines = ['{"type": "agent_start"}'] + [thinking(i) for i in range(54)]
            return FakeRpcProcess(stdout_lines=lines, returncode=1)
        return FakeRpcProcess(
            stdout_lines=['{"type": "agent_start"}', '{"type": "agent_end", "messages": []}'],
            returncode=0,
        )

    bridge = SandboxBridge(
        control_plane_url="http://control-plane",
        bridge_token="tok_test_secret",
        investigation_id=inv_id,
        task_brief="Retry test",
        process_factory=proc_factory,
        start_gateway_server=False,
        http_client=client,
        batch_size=50,
        max_retries=2,
    )
    bridge._on_gateway_event(
        EventType.summary_submitted,
        {"findings": "F", "next_step": "N", "evidence_refs": ["ev_1"]},
    )

    exit_code = await bridge.run()
    assert exit_code == 0
    assert spawn_count == 2

    all_events = [event for batch in received_batches for event in batch]
    seqs = [event["seq"] for event in all_events]
    # Every persisted event has a unique seq: no event was persisted twice.
    assert len(seqs) == len(set(seqs))
    summaries = [e for e in all_events if e["type"] == "summary_submitted"]
    assert len(summaries) == 1

    await client.aclose()


@pytest.mark.asyncio
async def test_completion_without_stdout_eof_finishes_promptly() -> None:
    """Verify a valid summary plus agent_end completes while stdout stays open."""
    inv_id = uuid4()
    received_events: list[dict[str, Any]] = []

    async def mock_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/internal/events":
            received_events.extend(json.loads(request.content.decode()))
            return httpx.Response(200, json={"ok": True})
        if request.url.path == "/internal/inbox":
            return httpx.Response(200, json={"messages": [], "next_cursor": 0})
        return httpx.Response(404)

    client = httpx.AsyncClient(transport=httpx.MockTransport(mock_handler))

    # The agent emits agent_start + agent_end but keeps stdout open indefinitely.
    fake_proc = FakeRpcProcess(
        stdout_lines=[
            '{"type": "agent_start"}',
            '{"type": "agent_end", "messages": []}',
        ],
        returncode=0,
        delay_s=0.05,
        stdout_stays_open=True,
    )
    bridge = SandboxBridge(
        control_plane_url="http://control-plane",
        bridge_token="tok_test_secret",
        investigation_id=inv_id,
        task_brief="Analyze trace",
        process_factory=lambda: asyncio.sleep(0, result=fake_proc),
        start_gateway_server=False,
        http_client=client,
        max_retries=0,
        shutdown_grace_s=0.5,
        child_stop_timeout_s=1.0,
    )
    # Valid summary arrives via the gateway callback (before agent_end here).
    bridge._on_gateway_event(
        EventType.summary_submitted,
        {"findings": "F", "next_step": "N", "evidence_refs": ["ev_1"]},
    )

    loop = asyncio.get_running_loop()
    start = loop.time()
    exit_code = await bridge.run()
    elapsed = loop.time() - start

    # Run finished promptly instead of blocking on stdout EOF.
    assert exit_code == 0
    assert elapsed < 5.0

    event_types = [e["type"] for e in received_events]
    assert "summary_submitted" in event_types
    assert "investigation_finished" in event_types
    # Cleanup: stdin was closed and the child was reaped.
    assert fake_proc.stdin.is_closing()
    assert fake_proc.terminated or fake_proc.returncode is not None

    await client.aclose()


@pytest.mark.asyncio
async def test_watchdog_aborts_hung_tool_and_recovers() -> None:
    """Verify a hung Prime tool execution is aborted and the run recovers.

    Models the real live failure: no summary exists when the tool hangs. The
    watchdog aborts once (official {"type":"abort"}), Prime's abort closes the
    old run, the recovery steer opens a fresh run, the recovery agent submits a
    summary through the gateway, and the run completes only after the fresh
    recovery agent_end. No summary was pre-set before the hang.
    """
    inv_id = uuid4()
    received_events: list[dict[str, Any]] = []

    async def mock_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/internal/events":
            received_events.extend(json.loads(request.content.decode()))
            return httpx.Response(200, json={"ok": True})
        if request.url.path == "/internal/inbox":
            return httpx.Response(200, json={"messages": [], "next_cursor": 0})
        return httpx.Response(404)

    client = httpx.AsyncClient(transport=httpx.MockTransport(mock_handler))

    def respond_to_abort(text: str, reader: FakeStreamReader) -> None:
        # Prime's abort ends the aborted run; the queued recovery instruction
        # opens a fresh one. The recovery run completes at the end of the test.
        if '"type":"abort"' in text:
            reader.append_line('{"type": "agent_end", "messages": []}')
            reader.append_line('{"type": "agent_start"}')

    fake_proc = FakeRpcProcess(
        stdout_lines=[
            '{"type": "agent_start"}',
            (
                '{"type": "tool_execution_start", "toolCallId": "tc_hung", '
                '"toolName": "trace.read", "args": {}}'
            ),
        ],
        returncode=0,
        stdout_stays_open=True,
        on_stdin=respond_to_abort,
    )

    bridge = SandboxBridge(
        control_plane_url="http://control-plane",
        bridge_token="tok_watchdog",
        investigation_id=inv_id,
        task_brief="Watchdog task",
        process_factory=lambda: asyncio.sleep(0, result=fake_proc),
        start_gateway_server=False,
        http_client=client,
        max_retries=0,
        tool_timeout_s=0.2,
        recovery_timeout_s=2.0,
        shutdown_grace_s=0.1,
        child_stop_timeout_s=0.5,
    )
    # No summary is pre-set: this models the real live failure.

    async def drive_recovery() -> None:
        # Wait until the abort's agent_end and the recovery agent_start have
        # both been consumed, then submit the summary through the gateway...
        for _ in range(400):
            if fake_proc.stdout.index >= 4:
                break
            await asyncio.sleep(0.01)
        assert fake_proc.stdout.index >= 4
        bridge._on_gateway_event(
            EventType.summary_submitted,
            {"findings": "F", "next_step": "N", "evidence_refs": ["ev_1"]},
        )
        # ...and only then let the recovery run finish with its own agent_end.
        fake_proc.stdout.append_line('{"type": "agent_end", "messages": []}')

    driver = asyncio.create_task(drive_recovery())
    exit_code = await bridge.run()
    await driver
    assert exit_code == 0

    commands = [json.loads(cmd.strip()) for cmd in fake_proc.received_stdin]
    abort_cmds = [c for c in commands if c.get("type") == "abort"]
    assert len(abort_cmds) == 1  # aborted exactly once, never re-aborted

    recovery = [c for c in commands if c.get("type") == "steer"]
    assert len(recovery) == 1
    assert "summary.submit" in recovery[0]["message"]
    assert "Do not retry" in recovery[0]["message"]

    warnings = [e for e in received_events if e["type"] == "warning"]
    assert len(warnings) == 1
    assert warnings[0]["payload"]["tool_call_ids"] == ["tc_hung"]
    assert warnings[0]["payload"]["recovery_asked"] is True

    event_types = [e["type"] for e in received_events]
    assert "summary_submitted" in event_types
    assert "investigation_finished" in event_types
    assert not any(e["type"] == "error" for e in received_events)

    await client.aclose()


@pytest.mark.asyncio
async def test_watchdog_recovery_expired_fails_deterministically() -> None:
    """Verify recovery that never completes fails visibly with no fake success."""
    inv_id = uuid4()
    received_events: list[dict[str, Any]] = []

    async def mock_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/internal/events":
            received_events.extend(json.loads(request.content.decode()))
            return httpx.Response(200, json={"ok": True})
        if request.url.path == "/internal/inbox":
            return httpx.Response(200, json={"messages": [], "next_cursor": 0})
        return httpx.Response(404)

    client = httpx.AsyncClient(transport=httpx.MockTransport(mock_handler))

    def never_recover(text: str, reader: FakeStreamReader) -> None:
        return None

    fake_proc = FakeRpcProcess(
        stdout_lines=[
            '{"type": "agent_start"}',
            (
                '{"type": "tool_execution_start", "toolCallId": "tc_hung", '
                '"toolName": "trace.read", "args": {}}'
            ),
        ],
        returncode=0,
        stdout_stays_open=True,
        on_stdin=never_recover,
    )

    bridge = SandboxBridge(
        control_plane_url="http://control-plane",
        bridge_token="tok_watchdog",
        investigation_id=inv_id,
        task_brief="Watchdog task",
        process_factory=lambda: asyncio.sleep(0, result=fake_proc),
        start_gateway_server=False,
        http_client=client,
        max_retries=0,
        tool_timeout_s=0.2,
        recovery_timeout_s=0.4,
        shutdown_grace_s=0.1,
        child_stop_timeout_s=0.5,
    )

    exit_code = await bridge.run()
    assert exit_code == 1

    errors = [e for e in received_events if e["type"] == "error"]
    assert len(errors) == 1
    assert errors[0]["payload"]["fatal"] is True
    assert "did not recover" in errors[0]["payload"]["error"]

    warnings = [e for e in received_events if e["type"] == "warning"]
    assert len(warnings) == 1

    commands = [json.loads(cmd.strip()) for cmd in fake_proc.received_stdin]
    assert len([c for c in commands if c.get("type") == "abort"]) == 1

    await client.aclose()


@pytest.mark.asyncio
async def test_watchdog_abort_agent_end_cannot_complete_recovery() -> None:
    """Red regression: exact order hung tool -> abort -> abort agent_end ->
    recovery agent_start -> summary_submit -> (no recovery agent_end).

    Prime's official abort emits its own agent_end; the recovery steer opens a
    fresh agent_start. A summary.submit before the recovery run's own agent_end
    must NOT complete the run. Without a fresh agent_end the run must fail on
    bounded recovery expiry -- never accept the abort's stale agent_end.
    """
    inv_id = uuid4()
    received_events: list[dict[str, Any]] = []

    async def mock_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/internal/events":
            received_events.extend(json.loads(request.content.decode()))
            return httpx.Response(200, json={"ok": True})
        if request.url.path == "/internal/inbox":
            return httpx.Response(200, json={"messages": [], "next_cursor": 0})
        return httpx.Response(404)

    client = httpx.AsyncClient(transport=httpx.MockTransport(mock_handler))

    def abort_then_new_run(text: str, reader: FakeStreamReader) -> None:
        # Prime's abort ends the old run; the recovery instruction opens a new
        # one. The recovery run never emits its own agent_end.
        if '"type":"abort"' in text:
            reader.append_line('{"type": "agent_end", "messages": []}')
            reader.append_line('{"type": "agent_start"}')

    fake_proc = FakeRpcProcess(
        stdout_lines=[
            '{"type": "agent_start"}',
            (
                '{"type": "tool_execution_start", "toolCallId": "tc_hung", '
                '"toolName": "trace.read", "args": {}}'
            ),
        ],
        returncode=0,
        stdout_stays_open=True,
        on_stdin=abort_then_new_run,
    )

    bridge = SandboxBridge(
        control_plane_url="http://control-plane",
        bridge_token="tok_watchdog",
        investigation_id=inv_id,
        task_brief="Watchdog task",
        process_factory=lambda: asyncio.sleep(0, result=fake_proc),
        start_gateway_server=False,
        http_client=client,
        max_retries=0,
        tool_timeout_s=0.2,
        recovery_timeout_s=0.4,
        shutdown_grace_s=0.1,
        child_stop_timeout_s=0.5,
    )

    async def submit_summary_after_recovery_start() -> None:
        # Deterministic ordering: wait until the abort's agent_end AND the
        # recovery agent_start have both been consumed, then submit the summary
        # (the recovery phase). No fresh agent_end will ever arrive.
        for _ in range(400):
            if fake_proc.stdout.index >= 4:
                break
            await asyncio.sleep(0.01)
        assert fake_proc.stdout.index >= 4
        bridge._on_gateway_event(
            EventType.summary_submitted,
            {"findings": "F", "next_step": "N", "evidence_refs": ["ev_1"]},
        )

    driver = asyncio.create_task(submit_summary_after_recovery_start())
    exit_code = await bridge.run()
    await driver

    # The abort's agent_end and the recovery summary must NOT combine to
    # complete: the run fails on bounded recovery expiry.
    assert exit_code == 1
    errors = [e for e in received_events if e["type"] == "error"]
    assert len(errors) == 1
    assert errors[0]["payload"]["fatal"] is True
    assert "did not recover" in errors[0]["payload"]["error"]
    # No investigation_finished is ever emitted without the fresh agent_end.
    assert not any(e["type"] == "investigation_finished" for e in received_events)

    await client.aclose()


@pytest.mark.asyncio
async def test_watchdog_fresh_recovery_agent_end_completes() -> None:
    """Verify the same recovery flow succeeds with the recovery run's own end.

    Same exact order as the red regression, plus the fresh recovery agent_end
    after summary_submit. Completion is then legitimate.
    """
    inv_id = uuid4()
    received_events: list[dict[str, Any]] = []

    async def mock_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/internal/events":
            received_events.extend(json.loads(request.content.decode()))
            return httpx.Response(200, json={"ok": True})
        if request.url.path == "/internal/inbox":
            return httpx.Response(200, json={"messages": [], "next_cursor": 0})
        return httpx.Response(404)

    client = httpx.AsyncClient(transport=httpx.MockTransport(mock_handler))

    def abort_then_new_run(text: str, reader: FakeStreamReader) -> None:
        if '"type":"abort"' in text:
            reader.append_line('{"type": "agent_end", "messages": []}')
            reader.append_line('{"type": "agent_start"}')

    fake_proc = FakeRpcProcess(
        stdout_lines=[
            '{"type": "agent_start"}',
            (
                '{"type": "tool_execution_start", "toolCallId": "tc_hung", '
                '"toolName": "trace.read", "args": {}}'
            ),
        ],
        returncode=0,
        stdout_stays_open=True,
        on_stdin=abort_then_new_run,
    )

    bridge = SandboxBridge(
        control_plane_url="http://control-plane",
        bridge_token="tok_watchdog",
        investigation_id=inv_id,
        task_brief="Watchdog task",
        process_factory=lambda: asyncio.sleep(0, result=fake_proc),
        start_gateway_server=False,
        http_client=client,
        max_retries=0,
        tool_timeout_s=0.2,
        recovery_timeout_s=2.0,
        shutdown_grace_s=0.1,
        child_stop_timeout_s=0.5,
    )

    async def recover_and_finish() -> None:
        for _ in range(400):
            if fake_proc.stdout.index >= 4:
                break
            await asyncio.sleep(0.01)
        assert fake_proc.stdout.index >= 4
        bridge._on_gateway_event(
            EventType.summary_submitted,
            {"findings": "F", "next_step": "N", "evidence_refs": ["ev_1"]},
        )
        # The recovery run now finishes with its own fresh agent_end.
        fake_proc.stdout.append_line('{"type": "agent_end", "messages": []}')

    driver = asyncio.create_task(recover_and_finish())
    exit_code = await bridge.run()
    await driver

    assert exit_code == 0
    event_types = [e["type"] for e in received_events]
    assert "summary_submitted" in event_types
    assert "investigation_finished" in event_types
    assert not any(e["type"] == "error" for e in received_events)

    await client.aclose()


@pytest.mark.asyncio
async def test_watchdog_run_budget_aborts_late_starting_tool() -> None:
    """Red regression: the overall run cutoff, not tool age, aborts a late tool.

    Measured live shape: outer=300, the hung tool begins at ~run second 140,
    per-tool timeout=120, recovery=60, reserve=60. Per-tool age would abort at
    second 260 -- too late for recovery and flush before Modal kills at second
    300. The overall run cutoff (300 - 60 recovery - 60 reserve = 180) must
    drive the abort instead. An injected monotonic clock keeps the test fast
    and deterministic.
    """
    inv_id = uuid4()
    received_events: list[dict[str, Any]] = []
    now = [0.0]
    abort_clocks: list[float] = []

    async def mock_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/internal/events":
            received_events.extend(json.loads(request.content.decode()))
            return httpx.Response(200, json={"ok": True})
        if request.url.path == "/internal/inbox":
            return httpx.Response(200, json={"messages": [], "next_cursor": 0})
        return httpx.Response(404)

    client = httpx.AsyncClient(transport=httpx.MockTransport(mock_handler))

    def abort_then_new_run(text: str, reader: FakeStreamReader) -> None:
        if '"type":"abort"' in text:
            abort_clocks.append(now[0])
            reader.append_line('{"type": "agent_end", "messages": []}')
            reader.append_line('{"type": "agent_start"}')

    fake_proc = FakeRpcProcess(
        stdout_lines=[
            '{"type": "agent_start"}',
            (
                '{"type": "tool_execution_start", "toolCallId": "tc_late", '
                '"toolName": "trace.read", "args": {}}'
            ),
        ],
        returncode=0,
        stdout_stays_open=True,
        on_stdin=abort_then_new_run,
    )

    def clock() -> float:
        return now[0]

    bridge = SandboxBridge(
        control_plane_url="http://control-plane",
        bridge_token="tok_watchdog",
        investigation_id=inv_id,
        task_brief="Watchdog run budget task",
        process_factory=lambda: asyncio.sleep(0, result=fake_proc),
        start_gateway_server=False,
        http_client=client,
        max_retries=0,
        tool_timeout_s=120,
        recovery_timeout_s=60,
        sandbox_timeout_s=300,
        time_source=clock,
        watchdog_interval_s=0.001,
        shutdown_grace_s=0.1,
        child_stop_timeout_s=0.5,
    )
    # The hung tool begins at ~run second 140 (measured live shape).
    now[0] = 140.0

    async def advance_clock() -> None:
        # Advance the fake clock through the cutoff (180) toward the bounded
        # recovery deadline (240 = 300 - reserve).
        for t in range(141, 245):
            now[0] = float(t)
            await asyncio.sleep(0.004)

    driver = asyncio.create_task(advance_clock())
    exit_code = await bridge.run()
    await driver

    # The abort is driven by the run cutoff ~180, never by tool age at 260.
    assert len(abort_clocks) == 1
    assert abort_clocks[0] < 260
    assert 170 <= abort_clocks[0] <= 200

    warnings = [e for e in received_events if e["type"] == "warning"]
    assert len(warnings) == 1
    assert warnings[0]["payload"]["reason"] == "run_budget"
    # The recovery deadline is bounded to outer - reserve = 240.
    assert bridge.recovery_deadline is not None
    assert bridge.recovery_deadline <= 241

    # No fresh recovery agent_end: the run fails deterministically on expiry.
    assert exit_code == 1
    errors = [e for e in received_events if e["type"] == "error"]
    assert len(errors) == 1

    await client.aclose()


@pytest.mark.asyncio
async def test_watchdog_run_budget_fires_without_active_tool() -> None:
    """The overall run budget aborts even when no tool is currently active."""
    inv_id = uuid4()
    received_events: list[dict[str, Any]] = []
    now = [0.0]
    abort_clocks: list[float] = []

    async def mock_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/internal/events":
            received_events.extend(json.loads(request.content.decode()))
            return httpx.Response(200, json={"ok": True})
        if request.url.path == "/internal/inbox":
            return httpx.Response(200, json={"messages": [], "next_cursor": 0})
        return httpx.Response(404)

    client = httpx.AsyncClient(transport=httpx.MockTransport(mock_handler))

    def record_abort(text: str, reader: FakeStreamReader) -> None:
        if '"type":"abort"' in text:
            abort_clocks.append(now[0])

    fake_proc = FakeRpcProcess(
        # The agent idles: no tool execution is ever active.
        stdout_lines=['{"type": "agent_start"}'],
        returncode=0,
        stdout_stays_open=True,
        on_stdin=record_abort,
    )

    def clock() -> float:
        return now[0]

    bridge = SandboxBridge(
        control_plane_url="http://control-plane",
        bridge_token="tok_watchdog",
        investigation_id=inv_id,
        task_brief="Watchdog idle budget task",
        process_factory=lambda: asyncio.sleep(0, result=fake_proc),
        start_gateway_server=False,
        http_client=client,
        max_retries=0,
        tool_timeout_s=120,
        recovery_timeout_s=60,
        sandbox_timeout_s=300,
        time_source=clock,
        watchdog_interval_s=0.001,
        shutdown_grace_s=0.1,
        child_stop_timeout_s=0.5,
    )
    now[0] = 100.0

    async def advance_clock() -> None:
        # Advance through the cutoff (~180) and past the bounded recovery
        # deadline (240 = 300 - reserve) so expiry is observed.
        for t in range(101, 245):
            now[0] = float(t)
            await asyncio.sleep(0.004)

    driver = asyncio.create_task(advance_clock())
    exit_code = await bridge.run()
    await driver

    # The cutoff (~180) fires even though no tool is active.
    assert len(abort_clocks) == 1
    assert 170 <= abort_clocks[0] <= 200
    warnings = [e for e in received_events if e["type"] == "warning"]
    assert len(warnings) == 1
    assert warnings[0]["payload"]["reason"] == "run_budget"
    # The run cannot complete (no summary/agent_end): deterministic fail.
    assert exit_code == 1
    assert any(e["type"] == "error" for e in received_events)

    await client.aclose()


@pytest.mark.asyncio
async def test_concurrent_flushes_serialize_no_event_loss() -> None:
    """Verify serialized flushes persist every seq once and never delete an unsent tail."""
    inv_id = uuid4()
    batches: list[list[int]] = []
    request_in_flight = asyncio.Event()
    release = asyncio.Event()

    async def mock_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/internal/events":
            seqs = [e["seq"] for e in json.loads(request.content.decode())]
            batches.append(seqs)
            request_in_flight.set()
            await release.wait()
            return httpx.Response(200, json={"ok": True})
        return httpx.Response(404)

    client = httpx.AsyncClient(transport=httpx.MockTransport(mock_handler))

    bridge = SandboxBridge(
        control_plane_url="http://control-plane",
        bridge_token="tok_test_secret",
        investigation_id=inv_id,
        http_client=client,
        batch_size=50,
    )
    for i in range(1, 11):
        bridge.record_event(EventType.thought, {"i": i})

    # First flush parks in-flight with events [1..10] snapshotted.
    first = asyncio.create_task(bridge.flush_events(client, force_all=True))
    await request_in_flight.wait()
    # An unsent tail is appended while the first flush is still in flight.
    bridge.record_event(EventType.thought, {"i": 11})
    second = asyncio.create_task(bridge.flush_events(client, force_all=True))
    release.set()
    results = await asyncio.gather(first, second)

    assert results == [True, True]
    all_seqs = [seq for batch in batches for seq in batch]
    # Serialization means the transport observes no duplicate batches...
    assert len(all_seqs) == len(set(all_seqs))
    # ...and every seq, including the tail appended mid-flush, was persisted.
    assert set(all_seqs) == set(range(1, 12))
    assert bridge.event_buffer == []

    await client.aclose()


@pytest.mark.asyncio
async def test_inbox_prompt_during_active_run_uses_steer_streaming() -> None:
    """Verify a relayed inbox prompt during an active run serializes with steer streaming."""
    inv_id = uuid4()
    received_events: list[dict[str, Any]] = []
    inbox_calls = 0

    async def mock_handler(request: httpx.Request) -> httpx.Response:
        nonlocal inbox_calls
        if request.url.path == "/internal/events":
            received_events.extend(json.loads(request.content.decode()))
            return httpx.Response(200, json={"ok": True})
        if request.url.path == "/internal/inbox":
            inbox_calls += 1
            if inbox_calls == 1:
                return httpx.Response(200, json={"messages": [], "next_cursor": 0})
            if inbox_calls == 2:
                # Wait until agent_start has activated the run.
                for _ in range(200):
                    if bridge.is_run_active:
                        break
                    await asyncio.sleep(0.01)
                return httpx.Response(
                    200,
                    json={
                        "messages": [{"id": "m1", "body": "Refocus on payment", "mode": "prompt"}],
                        "next_cursor": 1,
                    },
                )
            return httpx.Response(200, json={"messages": [], "next_cursor": 1})
        return httpx.Response(404)

    client = httpx.AsyncClient(transport=httpx.MockTransport(mock_handler))

    fake_proc = FakeRpcProcess(
        stdout_lines=[
            '{"type": "agent_start"}',
            '{"type": "agent_end", "messages": []}',
        ],
        returncode=0,
        delay_s=0.05,
    )
    bridge = SandboxBridge(
        control_plane_url="http://control-plane",
        bridge_token="tok_test_secret",
        investigation_id=inv_id,
        task_brief="Start investigation",
        process_factory=lambda: asyncio.sleep(0, result=fake_proc),
        start_gateway_server=False,
        http_client=client,
        inbox_poll_interval_s=0.01,
        max_retries=0,
    )
    bridge.summary_submitted = True

    exit_code = await bridge.run()
    assert exit_code == 0

    commands = [json.loads(cmd.strip()) for cmd in fake_proc.received_stdin]

    # The initial task brief is the first prompt, unchanged (no streamingBehavior).
    assert commands[0]["type"] == "prompt"
    assert commands[0]["message"] == "Start investigation"
    assert "streamingBehavior" not in commands[0]

    # The inbox prompt relayed while the run is active carries steer streaming.
    prompts = [c for c in commands[1:] if c["type"] == "prompt"]
    assert len(prompts) == 1
    assert prompts[0]["message"] == "Refocus on payment"
    assert prompts[0]["streamingBehavior"] == "steer"

    await client.aclose()


@pytest.mark.asyncio
async def test_rejected_rpc_response_fails_visibly_no_retry() -> None:
    """Verify a success:false RPC response surfaces as a fatal error and never replays."""
    inv_id = uuid4()
    received_events: list[dict[str, Any]] = []
    spawn_count = 0

    async def mock_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/internal/events":
            received_events.extend(json.loads(request.content.decode()))
            return httpx.Response(200, json={"ok": True})
        if request.url.path == "/internal/inbox":
            return httpx.Response(200, json={"messages": [], "next_cursor": 0})
        return httpx.Response(404)

    client = httpx.AsyncClient(transport=httpx.MockTransport(mock_handler))

    secret_token = "tok_reject_secret"

    async def proc_factory() -> FakeRpcProcess:
        nonlocal spawn_count
        spawn_count += 1
        return FakeRpcProcess(
            stdout_lines=[
                '{"type": "agent_start"}',
                json.dumps(
                    {
                        "type": "response",
                        "success": False,
                        "error": f"model busy with token {secret_token}",
                    }
                ),
            ],
            returncode=0,
        )

    bridge = SandboxBridge(
        control_plane_url="http://control-plane",
        bridge_token=secret_token,
        investigation_id=inv_id,
        task_brief="Reject test",
        process_factory=proc_factory,
        start_gateway_server=False,
        http_client=client,
        max_retries=3,
    )

    exit_code = await bridge.run()
    assert exit_code == 1
    assert bridge.protocol_failure is True
    assert spawn_count == 1  # protocol failures never retry / replay

    errors = [e for e in received_events if e["type"] == "error"]
    assert len(errors) >= 1
    assert errors[0]["payload"]["fatal"] is True
    assert "rejected" in errors[0]["payload"]["error"].lower()
    assert "model busy" in errors[0]["payload"]["error"]
    # Remote error text is sanitized with the existing redaction.
    assert secret_token not in json.dumps(errors)
    assert "[REDACTED]" in json.dumps(errors)

    # A rejected command response is never replayed as a normal message event.
    msg_events = [e for e in received_events if e["type"] == "message"]
    assert not any("model busy" in str(e["payload"]) for e in msg_events)

    await client.aclose()


def test_main_requires_investigation_id(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify main() fails fast instead of silently generating an identity."""
    monkeypatch.delenv("INVESTIGATION_ID", raising=False)

    def fail_construction(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("SandboxBridge constructed without INVESTIGATION_ID")

    monkeypatch.setattr("app.domain.agent_runner.bridge.SandboxBridge", fail_construction)
    assert main() == 2


def test_main_rejects_invalid_investigation_id(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify main() rejects a non-UUID INVESTIGATION_ID."""
    monkeypatch.setenv("INVESTIGATION_ID", "not-a-uuid")

    def fail_construction(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("SandboxBridge constructed with invalid INVESTIGATION_ID")

    monkeypatch.setattr("app.domain.agent_runner.bridge.SandboxBridge", fail_construction)
    assert main() == 2


def test_main_parses_investigation_id(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify main() parses a valid INVESTIGATION_ID into the bridge."""
    inv_id = uuid4()
    monkeypatch.setenv("INVESTIGATION_ID", str(inv_id))
    monkeypatch.setenv("CONTROL_PLANE_CALLBACK_URL", "http://control-plane")
    monkeypatch.setenv("BRIDGE_TOKEN", "tok_main")
    captured: dict[str, Any] = {}

    class FakeBridge:
        def __init__(self, **kwargs: Any) -> None:
            captured.update(kwargs)

        async def run(self) -> int:
            return 7

    monkeypatch.setattr("app.domain.agent_runner.bridge.SandboxBridge", FakeBridge)
    assert main() == 7
    assert captured["investigation_id"] == inv_id


def test_main_parses_watchdog_timeouts_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify the Modal-injected watchdog env reaches the bridge watchdog."""
    inv_id = uuid4()
    monkeypatch.setenv("INVESTIGATION_ID", str(inv_id))
    monkeypatch.setenv("CONTROL_PLANE_CALLBACK_URL", "http://control-plane")
    monkeypatch.setenv("BRIDGE_TOKEN", "tok_main")
    monkeypatch.setenv("TOOL_TIMEOUT_S", "120")
    monkeypatch.setenv("TOOL_RECOVERY_TIMEOUT_S", "60")
    monkeypatch.setenv("SANDBOX_TIMEOUT_S", "300")
    captured: dict[str, Any] = {}

    class FakeBridge:
        def __init__(self, **kwargs: Any) -> None:
            captured.update(kwargs)

        async def run(self) -> int:
            return 7

    monkeypatch.setattr("app.domain.agent_runner.bridge.SandboxBridge", FakeBridge)
    assert main() == 7
    assert captured["tool_timeout_s"] == 120.0
    assert captured["recovery_timeout_s"] == 60.0
    assert captured["sandbox_timeout_s"] == 300.0


def test_main_rejects_non_numeric_watchdog_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify non-numeric watchdog env fails fast in main()."""
    inv_id = uuid4()
    monkeypatch.setenv("INVESTIGATION_ID", str(inv_id))
    monkeypatch.setenv("CONTROL_PLANE_CALLBACK_URL", "http://control-plane")
    monkeypatch.setenv("BRIDGE_TOKEN", "tok_main")
    monkeypatch.setenv("TOOL_TIMEOUT_S", "not-a-number")

    def fail_construction(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("SandboxBridge constructed with invalid watchdog env")

    monkeypatch.setattr("app.domain.agent_runner.bridge.SandboxBridge", fail_construction)
    assert main() == 2


def test_main_parses_sandbox_timeout_and_rejects_non_numeric(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify SANDBOX_TIMEOUT_S reaches the bridge and non-numeric values fail."""
    inv_id = uuid4()
    monkeypatch.setenv("INVESTIGATION_ID", str(inv_id))
    monkeypatch.setenv("CONTROL_PLANE_CALLBACK_URL", "http://control-plane")
    monkeypatch.setenv("BRIDGE_TOKEN", "tok_main")
    captured: dict[str, Any] = {}

    class FakeBridge:
        def __init__(self, **kwargs: Any) -> None:
            captured.update(kwargs)

        async def run(self) -> int:
            return 7

    def fail_for_invalid(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("SandboxBridge constructed with invalid SANDBOX_TIMEOUT_S")

    # 1. Numeric SANDBOX_TIMEOUT_S is parsed and passed through.
    monkeypatch.setenv("SANDBOX_TIMEOUT_S", "300")
    monkeypatch.setattr("app.domain.agent_runner.bridge.SandboxBridge", FakeBridge)
    assert main() == 7
    assert captured["sandbox_timeout_s"] == 300.0

    # 2. Non-numeric SANDBOX_TIMEOUT_S fails fast.
    monkeypatch.setenv("SANDBOX_TIMEOUT_S", "bogus")
    monkeypatch.setattr("app.domain.agent_runner.bridge.SandboxBridge", fail_for_invalid)
    assert main() == 2

    # 3. Absent SANDBOX_TIMEOUT_S means no overall run cutoff.
    monkeypatch.delenv("SANDBOX_TIMEOUT_S", raising=False)
    monkeypatch.setattr("app.domain.agent_runner.bridge.SandboxBridge", FakeBridge)
    assert main() == 7
    assert captured["sandbox_timeout_s"] is None
