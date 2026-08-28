"""Offline end-to-end test for the complete Phase 8 investigation lifecycle.

Drives the real FastAPI application, repository persistence, chat inbox,
World Gateway callback, SSE replay, summary submission, and FakeRunner
termination through a real SandboxBridge.run() loop backed by a fake
Prime RPC process. Fully offline: the app is served through an in-process
ASGI transport, so no database or network is required.
"""

import asyncio
import json
from uuid import UUID

import httpx
from tests.fakes.investigation_repository import InMemoryInvestigationRepository
from tests.fakes.runners import FakeRunner

from app.api.dependencies import get_investigation_service
from app.config import Settings
from app.domain.agent_runner.bridge import SandboxBridge
from app.domain.agent_runner.gateway import create_gateway_app
from app.domain.investigation.service import InvestigationService
from app.main import create_app


class FakeStreamWriter:
    """Minimal stdin pipe double for the bridge's subprocess contract."""

    def __init__(self, buffer: list[str]) -> None:
        self.buffer = buffer
        self._closing = False

    def write(self, data: bytes) -> None:
        self.buffer.append(data.decode("utf-8"))

    async def drain(self) -> None:
        pass

    def close(self) -> None:
        self._closing = True

    def is_closing(self) -> bool:
        return self._closing


class FakeStreamReader:
    """Minimal stdout/stderr pipe double that replays scripted lines."""

    def __init__(self, lines: list[str], delay_s: float = 0.0) -> None:
        self.lines = list(lines)
        self.delay_s = delay_s
        self.index = 0

    def at_eof(self) -> bool:
        return self.index >= len(self.lines)

    async def readline(self) -> bytes:
        if self.at_eof():
            return b""
        line = self.lines[self.index]
        self.index += 1
        if self.delay_s > 0:
            await asyncio.sleep(self.delay_s)
        if not line.endswith("\n"):
            line += "\n"
        return line.encode("utf-8")


class FakeRpcProcess:
    """Smallest fake Prime Agent RPC process satisfying the bridge contract."""

    def __init__(
        self,
        stdout_lines: list[str] | None = None,
        *,
        returncode: int = 0,
        delay_s: float = 0.0,
    ) -> None:
        self.received_stdin: list[str] = []
        self.returncode = returncode
        self.delay_s = delay_s
        self.terminated = False
        self.stdin = FakeStreamWriter(self.received_stdin)
        self.stdout = FakeStreamReader(stdout_lines or [], delay_s=delay_s)
        self.stderr = FakeStreamReader([])

    async def wait(self) -> int:
        if self.delay_s > 0:
            await asyncio.sleep(self.delay_s)
        return self.returncode

    def terminate(self) -> None:
        self.terminated = True


async def test_offline_investigation_lifecycle_e2e() -> None:
    """Full offline end-to-end verification of Phase 8 investigation runner."""
    repo = InMemoryInvestigationRepository()
    fake_runner = FakeRunner()
    service = InvestigationService(repo, runner=fake_runner)

    settings = Settings(
        database_url="postgresql://user:pass@localhost:5432/test",
        environment="test",
        modal_enabled=False,
        _env_file=None,
    )
    app = create_app(settings)
    app.dependency_overrides[get_investigation_service] = lambda: service

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

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://control",
    ) as client:
        # 1. Start investigation via the real API route
        create_resp = await client.post("/api/v1/investigations", json=create_payload)
        assert create_resp.status_code == 202
        inv_data = create_resp.json()
        inv_id = UUID(inv_data["id"])
        bridge_token = inv_data["bridge_token"]
        assert bridge_token
        task_brief = inv_data["task_brief"]

        # 2. Drive a real bridge run with a fake Prime RPC process
        message_line = (
            '{"type": "message_update", "message": {"role": "assistant", "content": []}, '
            '"assistantMessageEvent": {"type": "text_delta", "delta": "Understood"}}'
        )
        fake_proc = FakeRpcProcess(
            stdout_lines=[
                '{"type": "agent_start"}',
                message_line,
                '{"type": "agent_end", "messages": []}',
            ],
            returncode=0,
            delay_s=0.15,
        )
        gateway_app = create_gateway_app(bridge_token)
        bridge = SandboxBridge(
            control_plane_url="http://control",
            bridge_token=bridge_token,
            investigation_id=inv_id,
            task_brief=task_brief,
            process_factory=lambda: asyncio.sleep(0, result=fake_proc),
            gateway_app=gateway_app,
            start_gateway_server=False,
            http_client=client,
            inbox_poll_interval_s=0.01,
            max_retries=0,
        )

        async def drive_chat() -> httpx.Response:
            # Post the user message only after agent_start activated the run so
            # the bridge relays it as steer through the real inbox endpoint.
            for _ in range(500):
                if bridge.is_run_active:
                    break
                await asyncio.sleep(0.01)
            return await client.post(
                f"/api/v1/investigations/{inv_id}/messages",
                json={
                    "body": "Focus on duplicate charge webhook calls",
                    "mode": "steer",
                },
            )

        async def drive_gateway() -> None:
            headers = {"Authorization": f"Bearer {bridge_token}"}
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=gateway_app),
                base_url="http://gw",
            ) as gw:
                for _ in range(500):
                    if bridge.is_run_active:
                        break
                    await asyncio.sleep(0.01)
                await asyncio.sleep(0.05)
                read_resp = await gw.post(
                    "/tools/evidence.read",
                    json={"arguments": {"trace_id": "tr_e2e_001"}},
                    headers=headers,
                )
                assert read_resp.status_code == 200
                summary_resp = await gw.post(
                    "/tools/summary.submit",
                    json={
                        "arguments": {
                            "findings": (
                                "Root cause: webhook handler lacks idempotency key deduplication."
                            ),
                            "next_step": (
                                "Add Redis deduplication lock around transaction processing."
                            ),
                            "evidence_refs": ["ev_1"],
                        }
                    },
                    headers=headers,
                )
                assert summary_resp.status_code == 200

        exit_code, chat_resp, _gateway_result = await asyncio.gather(
            bridge.run(),
            drive_chat(),
            drive_gateway(),
        )

        # 3. Bridge completed cleanly and the chat reached stdin via the inbox
        assert exit_code == 0
        assert chat_resp.status_code == 201
        assert chat_resp.json()["mode"] == "steer"
        assert chat_resp.json()["body"] == "Focus on duplicate charge webhook calls"
        assert len(repo.messages.get(inv_id, [])) == 1

        # 4. Task brief is the first command; one user chat follows it
        commands = [json.loads(cmd.strip()) for cmd in fake_proc.received_stdin]
        assert commands[0]["type"] == "prompt"
        assert commands[0]["message"] == task_brief
        assert [cmd["type"] for cmd in commands[1:]] == ["steer"]
        assert commands[1]["message"] == "Focus on duplicate charge webhook calls"

        # 5. Persisted events are ordered and duplicate-free with one summary
        persisted = await repo.get_events(inv_id, after_seq=0)
        assert [e.seq for e in persisted] == sorted(e.seq for e in persisted)
        assert len({e.seq for e in persisted}) == len(persisted)
        persisted_types = [e.type for e in persisted]
        assert persisted_types.count("summary_submitted") == 1
        assert persisted_types.count("investigation_finished") == 1
        for expected in (
            "investigation_started",
            "agent_ready",
            "message",
            "tool_call",
            "tool_result",
            "summary_submitted",
            "investigation_finished",
        ):
            assert expected in persisted_types

        # 6. SSE replay through the real endpoint matches persistence, no duplicates
        sse_resp = await client.get(f"/api/v1/investigations/{inv_id}/events")
        assert sse_resp.status_code == 200
        sse_events: list[tuple[int, str]] = []
        current_id: int | None = None
        current_type = ""
        for raw_line in sse_resp.text.splitlines():
            if raw_line.startswith("id: "):
                current_id = int(raw_line[4:])
            elif raw_line.startswith("event: "):
                current_type = raw_line[7:]
            elif raw_line.startswith("data: ") and current_id is not None:
                sse_events.append((current_id, current_type))
                current_id = None
        assert len(sse_events) == len(persisted)
        assert [seq for seq, _ in sse_events] == [e.seq for e in persisted]
        assert [event_type for _, event_type in sse_events] == persisted_types
        assert [event_type for _, event_type in sse_events].count("summary_submitted") == 1

        # 7. Final state, exactly one summary, offline loopback callback,
        #    and FakeRunner sandbox termination
        final_resp = await client.get(f"/api/v1/investigations/{inv_id}")
        assert final_resp.status_code == 200
        final_data = final_resp.json()
        assert final_data["status"] == "completed"
        assert final_data["summary"] is not None
        assert "idempotency key deduplication" in final_data["summary"]["findings"]
        assert "Redis deduplication lock" in final_data["summary"]["next_step"]

        assert len(fake_runner.terminated_handles) == 1
        assert fake_runner.specs[0].callback_base_url == "http://127.0.0.1:8000"
        assert fake_runner.specs[0].outbound_domain_allowlist == []
