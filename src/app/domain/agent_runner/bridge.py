"""In-sandbox bridge process relaying messages and streaming investigation events.

Responsibilities:
1. Start and supervise the loopback World Gateway on 127.0.0.1:8001 using a
   per-run loopback WORLD_GATEWAY_TOKEN that is separate from the control-plane
   BRIDGE_TOKEN. Prime Agent never receives BRIDGE_TOKEN.
2. Start and supervise Prime Agent in line-delimited RPC mode
   (`prime-agent --mode rpc --no-session`) through an injectable process factory
   with stdin/stdout/stderr pipes. The child runs under a deliberately sanitized
   environment: the World Gateway token, runtime basics, and model-provider
   credentials only. BRIDGE_TOKEN, CONTROL_PLANE_CALLBACK_URL, task/control-plane
   data, database credentials, and unrelated secrets are excluded. IPython and
   the nine World Gateway extension tools stay enabled (never
   ``--no-builtin-tools``).
3. Send the task brief as the first `prompt` command to prime-agent stdin.
4. Long-poll the control plane (GET /internal/inbox) and relay prompt, steer, and
   follow_up messages to stdin, applying the steer downgrade rule.
5. Read official JSONL stdout, map official events to domain events, batch up to
   50 events or 500ms, and POST to POST /internal/events.
6. Emit periodic heartbeat events.
7. Attach one event callback to the World Gateway so actual gateway requests
   produce authoritative tool_call and tool_result events. The gateway context
   contains only truthful metadata the bridge can prove; state/evidence/diff
   data that is not compiled returns an explicit unavailable result.
8. Validate completion: clean success (exit 0) requires BOTH a valid summary.submit
   AND an agent_end event. Duplicate summary calls never duplicate summary events.
9. Non-JSON RPC stdout is a protocol failure: emit a visible error and return nonzero.
10. Retry crashes up to `max_retries` ONLY when the failure occurs before any
    state-changing gateway tool call. Never replay after mutation.
11. A bounded watchdog aborts the active Prime operation when EITHER a tracked
    tool execution (by toolCallId from official tool_execution_start/end events)
    runs past `tool_timeout_s`, OR the overall run cutoff
    (`sandbox_timeout_s` - recovery - reserve, derived from the injected
    SANDBOX_TIMEOUT_S) is reached while the run is still incomplete -- covering
    tools that start late in the run. It sends the official `{"type":"abort"}`
    RPC command once, persists a warning, invalidates any stale agent_end, and
    makes one bounded recovery attempt instructing Prime to submit its summary
    without retrying the timed-out operation. The recovery deadline is bounded
    to `sandbox_timeout_s` - reserve. If recovery cannot reach a valid summary
    plus a fresh agent_end within the deadline, the run fails deterministically;
    completion is never faked.
12. Ensure complete resource cleanup, a bounded redacted stderr tail, and a final
    flush on every exit path.
"""

import asyncio
import json
import logging
import os
import secrets
import sys
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from typing import Any, Literal
from uuid import UUID

import httpx
import uvicorn
from fastapi import FastAPI

from app.domain.agent_runner.gateway import STATE_CHANGING_TOOLS, create_gateway_app
from app.domain.agent_runner.prime_rpc import (
    StreamingBehavior,
    format_abort_command,
    format_rpc_command,
    parse_rpc_output_line,
    parse_rpc_response,
)
from app.domain.runner.schemas import (
    DEFAULT_TOOL_RECOVERY_TIMEOUT_S,
    DEFAULT_TOOL_TIMEOUT_S,
    MODAL_RUN_RESERVE_S,
    EventType,
    RunnerEvent,
)

logger = logging.getLogger("sandbox.bridge")

ProcessFactory = Callable[[], Awaitable[asyncio.subprocess.Process]]

# Non-secret runtime variables the Prime Agent child process needs. Everything
# else in the container environment is deliberately excluded.
CHILD_RUNTIME_ENV_KEYS: frozenset[str] = frozenset(
    {
        "PATH",
        "HOME",
        "USER",
        "SHELL",
        "TERM",
    }
)

# Model-provider credential environment variables propagated into the sanitized
# Prime child environment. Provider credentials under any other name (for
# example a custom OPENAI-compatible provider) are NOT propagated; extend this
# set when adding a provider. This is the documented MVP boundary: the provider
# credential remains visible to Prime/IPython until a provider proxy is added.
PROVIDER_CREDENTIAL_ENV_KEYS: frozenset[str] = frozenset(
    {
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "GOOGLE_API_KEY",
        "GEMINI_API_KEY",
        "XAI_API_KEY",
        "GROQ_API_KEY",
        "DEEPSEEK_API_KEY",
        "MISTRAL_API_KEY",
        "TOGETHER_API_KEY",
        "OPENROUTER_API_KEY",
        "AZURE_OPENAI_API_KEY",
    }
)

# Bridge-level watchdog timeouts default from the shared SandboxSpec contract
# so they can never drift from the Modal-enforced invariant. The tool timeout is
# always below the Modal outer timeout because SandboxSpec validation rejects any
# watchdog budget (tool + recovery + reserve) that does not fit the outer
# lifetime. The container env TOOL_TIMEOUT_S / TOOL_RECOVERY_TIMEOUT_S
# (injected by ModalRunner) take precedence; these defaults are only the
# no-env fallback.

RECOVERY_INSTRUCTION = (
    "Your last tool execution was aborted because it exceeded this run's tool "
    "timeout. Do not retry that operation. Use the evidence you have already "
    "collected during this investigation and submit your final summary now via "
    "the summary.submit tool."
)

# Container environment variables the bridge turns into truthful World Gateway
# context metadata. Keys are not secrets and never reach the Prime child env.
WORLD_ENV_TO_CONTEXT: dict[str, str] = {
    "WORLD_ID": "world_id",
    "WORLD_VERSION": "world_version",
    "SLICE_NAME": "slice_name",
    "FIXTURE_BUNDLE_REF": "fixture_bundle_ref",
    "SLICE_PROVENANCE": "provenance",
}


class SandboxBridge:
    """Relays messages inward and progress events outward inside a Modal sandbox."""

    def __init__(
        self,
        control_plane_url: str,
        bridge_token: str,
        investigation_id: UUID,
        *,
        task_brief: str = "",
        gateway_port: int = 8001,
        gateway_app: FastAPI | None = None,
        start_gateway_server: bool = True,
        process_factory: ProcessFactory | None = None,
        http_client: httpx.AsyncClient | None = None,
        world_gateway_token: str | None = None,
        world_context: dict[str, Any] | None = None,
        child_env: dict[str, str] | None = None,
        max_retries: int = 3,
        batch_size: int = 50,
        batch_interval_s: float = 0.5,
        heartbeat_interval_s: float = 15.0,
        inbox_poll_interval_s: float = 1.0,
        shutdown_grace_s: float = 2.0,
        child_stop_timeout_s: float = 5.0,
        tool_timeout_s: float = DEFAULT_TOOL_TIMEOUT_S,
        recovery_timeout_s: float = DEFAULT_TOOL_RECOVERY_TIMEOUT_S,
        sandbox_timeout_s: float | None = None,
        time_source: Callable[[], float] | None = None,
        watchdog_interval_s: float = 0.2,
    ) -> None:
        self.control_plane_url = control_plane_url.rstrip("/")
        self.bridge_token = bridge_token
        # Per-run loopback token authenticating World Gateway requests. It is
        # distinct from the control-plane BRIDGE_TOKEN and is the only token
        # Prime Agent receives (through WORLD_GATEWAY_TOKEN in the child env).
        self.world_gateway_token = world_gateway_token or secrets.token_urlsafe(32)
        self.investigation_id = investigation_id
        self.task_brief = task_brief
        self.gateway_port = gateway_port
        self._gateway_app = gateway_app
        self.start_gateway_server = start_gateway_server
        self._process_factory = process_factory
        self._custom_client = http_client
        self.world_context = world_context or {}
        self._explicit_child_env = child_env
        self.max_retries = max_retries
        self.batch_size = batch_size
        self.batch_interval_s = batch_interval_s
        self.heartbeat_interval_s = heartbeat_interval_s
        self.inbox_poll_interval_s = inbox_poll_interval_s
        self.shutdown_grace_s = shutdown_grace_s
        self.child_stop_timeout_s = child_stop_timeout_s
        self.tool_timeout_s = tool_timeout_s
        self.recovery_timeout_s = recovery_timeout_s
        # Optional injected monotonic clock for deterministic tests. When unset,
        # the asyncio loop's monotonic clock is used.
        self._time_source = time_source
        self.watchdog_interval_s = watchdog_interval_s

        # Overall run budget: the sandbox outer lifetime as injected by the
        # runner (SANDBOX_TIMEOUT_S), plus the fixed run reserve. The watchdog
        # must force an abort/recovery by `overall_cutoff` and finish recovery
        # (or fail) by `overall_deadline` so event flush and child cleanup
        # complete before Modal kills the container -- even when a hung tool
        # starts late in the run, as observed live (tool at ~140s of a 300s
        # run, killed at ~299s).
        self.sandbox_timeout_s = (
            sandbox_timeout_s if sandbox_timeout_s is not None and sandbox_timeout_s > 0 else None
        )
        self.run_reserve_s = MODAL_RUN_RESERVE_S
        self.overall_cutoff: float | None
        self.overall_deadline: float | None
        if self.sandbox_timeout_s is not None:
            self.overall_cutoff = max(
                self.sandbox_timeout_s - self.recovery_timeout_s - self.run_reserve_s,
                0.0,
            )
            self.overall_deadline = max(self.sandbox_timeout_s - self.run_reserve_s, 0.0)
        else:
            self.overall_cutoff = None
            self.overall_deadline = None

        self.seq = 0
        self.cursor = 0
        self.is_run_active = False
        self.event_buffer: list[RunnerEvent] = []
        self.backoff_s = 1.0
        self.crash_count = 0
        self.summary_submitted = False
        self.agent_end_received = False
        self.has_mutated = False
        self.protocol_failure = False
        self.stderr_tail: list[str] = []
        # Watchdog state: official tool_execution_start/end tracked by toolCallId.
        self.active_tools: dict[str, float] = {}
        self.tool_timeout_hit = False
        self.abort_sent = False
        self.recovery_deadline: float | None = None
        self.watchdog_reason: str | None = None
        # True from the watchdog abort until the recovery run's own agent_start.
        # While open, an agent_end emitted by the abort closes only the aborted
        # run: it never emits investigation_finished nor satisfies completion.
        self.aborted_run_open = False

        self._gateway_server: uvicorn.Server | None = None
        self._gateway_task: asyncio.Task[None] | None = None
        self._child_process: asyncio.subprocess.Process | None = None
        self._flush_lock = asyncio.Lock()
        self._completion_event: asyncio.Event | None = None

    def _now(self) -> float:
        """Return the (possibly injected) monotonic clock in seconds."""
        if self._time_source is not None:
            return self._time_source()
        return asyncio.get_running_loop().time()

    @property
    def auth_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.bridge_token}"}

    def next_seq(self) -> int:
        self.seq += 1
        return self.seq

    def redact_secrets(self, text: str) -> str:
        """Redact bridge and gateway tokens and sensitive substrings from text."""
        if not text:
            return ""
        if self.bridge_token:
            text = text.replace(self.bridge_token, "[REDACTED]")
        if self.world_gateway_token:
            text = text.replace(self.world_gateway_token, "[REDACTED]")
        return text

    def build_child_env(self) -> dict[str, str]:
        """Build the deliberately sanitized environment for Prime Agent.

        Only non-secret runtime basics, model-provider credentials, and the
        World Gateway loopback wiring survive. BRIDGE_TOKEN,
        CONTROL_PLANE_CALLBACK_URL, task/control-plane data, database
        credentials, and unrelated secrets are excluded so Prime Agent and
        anything it spawns (including IPython) cannot read them.
        """
        if self._explicit_child_env is not None:
            return dict(self._explicit_child_env)

        env: dict[str, str] = {}
        for key in sorted(CHILD_RUNTIME_ENV_KEYS):
            value = os.environ.get(key)
            if value:
                env[key] = value
        for key, value in os.environ.items():
            if key in PROVIDER_CREDENTIAL_ENV_KEYS and value:
                env[key] = value

        env["WORLD_GATEWAY_URL"] = f"http://127.0.0.1:{self.gateway_port}"
        env["WORLD_GATEWAY_TOKEN"] = self.world_gateway_token
        return env

    def _build_gateway_context(self) -> dict[str, Any]:
        """Compose the truthful World Gateway context metadata.

        Only values the bridge can prove are included: the investigation ID,
        task reference length, world/slice references, and any trace reference
        passed through the container environment. State, diffs, and evidence
        are deliberately absent so the gateway reports them as unavailable.
        """
        context: dict[str, Any] = {
            "investigation_id": str(self.investigation_id),
            "task_brief_length": len(self.task_brief),
        }
        context.update(self.world_context)
        return context

    def translate_message_mode(
        self,
        mode: Literal["prompt", "steer", "follow_up"],
    ) -> Literal["prompt", "steer", "follow_up"]:
        """Translate chat message mode, applying the steer downgrade rule."""
        if mode == "steer" and not self.is_run_active:
            # Downgrade steer to follow_up if no run is currently active
            return "follow_up"
        return mode

    def process_incoming_chat(
        self,
        body: str,
        mode: Literal["prompt", "steer", "follow_up"],
    ) -> tuple[Literal["prompt", "steer", "follow_up"], str]:
        """Process chat message and produce effective mode and formatted RPC wire input."""
        effective_mode = self.translate_message_mode(mode)
        if effective_mode == "prompt":
            self.is_run_active = True
        return effective_mode, format_rpc_command(body, mode=effective_mode)

    def record_event(
        self,
        event_type: EventType,
        payload: dict[str, Any] | None = None,
    ) -> RunnerEvent:
        """Create and buffer a typed RunnerEvent."""
        event = RunnerEvent(
            investigation_id=self.investigation_id,
            seq=self.next_seq(),
            type=event_type,
            payload=payload or {},
            emitted_at=datetime.now(timezone.utc),
        )
        self.event_buffer.append(event)
        return event

    def _on_gateway_event(self, event_type: EventType, payload: dict[str, Any] | None) -> None:
        """Handle authoritative events emitted by the World Gateway."""
        if event_type == EventType.tool_call and payload:
            tool_name = str(payload.get("tool", ""))
            if tool_name in STATE_CHANGING_TOOLS:
                self.has_mutated = True

        if event_type == EventType.summary_submitted:
            self.summary_submitted = True
            self._maybe_complete_attempt()

        self.record_event(event_type, payload)

    def _maybe_complete_attempt(self) -> None:
        """Set the per-attempt completion signal once BOTH completion conditions hold.

        A run succeeds only after a valid summary.submit AND an agent_end event
        have both occurred. The signal fires regardless of which arrives second
        (gateway callback or stdout handler), so the run loop never blocks on
        stdout EOF before testing success.
        """
        if self.summary_submitted and self.agent_end_received:
            completion_event = self._completion_event
            if completion_event is not None:
                completion_event.set()

    async def flush_events(
        self,
        client: httpx.AsyncClient,
        *,
        force_all: bool = False,
    ) -> bool:
        """Post buffered events to control plane in batches.

        Returns True only when the entire buffer was delivered successfully.

        The entire snapshot/POST/eviction operation is serialized by a
        bridge-owned lock so concurrent flushes cannot both snapshot the same
        events and then delete an unsent tail appended mid-flight.
        """
        async with self._flush_lock:
            while self.event_buffer:
                batch_count = (
                    len(self.event_buffer)
                    if force_all
                    else min(len(self.event_buffer), self.batch_size)
                )
                batch_to_send = list(self.event_buffer[:batch_count])
                payload = [event.model_dump(mode="json") for event in batch_to_send]

                try:
                    resp = await client.post(
                        f"{self.control_plane_url}/internal/events",
                        json=payload,
                        headers=self.auth_headers,
                        timeout=10.0,
                    )
                    if resp.is_success:
                        del self.event_buffer[: len(batch_to_send)]
                    else:
                        logger.warning("Event flush returned HTTP %s", resp.status_code)
                        return False
                except Exception as exc:
                    logger.warning("Failed to flush events to control plane: %s", exc)
                    return False

                if not force_all:
                    break

            return not self.event_buffer

    async def poll_inbox(self, client: httpx.AsyncClient) -> list[dict[str, Any]]:
        """Long-poll control plane for new user messages."""
        try:
            resp = await client.get(
                f"{self.control_plane_url}/internal/inbox",
                params={"cursor": self.cursor},
                headers=self.auth_headers,
                timeout=30.0,
            )
            resp.raise_for_status()
            self.backoff_s = 1.0  # Reset backoff on success
            data = resp.json()
            raw_messages = data.get("messages", [])
            messages: list[dict[str, Any]] = [m for m in raw_messages if isinstance(m, dict)]
            if "next_cursor" in data:
                self.cursor = data["next_cursor"]
            elif messages:
                self.cursor += len(messages)
            return messages
        except Exception as exc:
            logger.warning("Inbox poll failed: %s. Backing off %ss", exc, self.backoff_s)
            await asyncio.sleep(self.backoff_s)
            self.backoff_s = min(self.backoff_s * 2.0, 30.0)

        return []

    async def handle_stdout_line(
        self,
        line: str,
        client: httpx.AsyncClient,
    ) -> bool:
        """Parse Prime Agent stdout line, buffer event, and update run lifecycle state.

        Non-JSON output on RPC stdout is a protocol failure: a visible error event
        is recorded and True is returned so the run loop stops with a nonzero exit.

        Official RPC command responses (type == "response") stay filtered from the
        normal domain event mapping, but a response with success:false is a fatal
        protocol failure: the remote error is sanitized, a visible fatal error
        event is recorded, and the attempt is stopped without replay.
        """
        stripped = line.strip()
        if stripped:
            try:
                json.loads(stripped)
            except json.JSONDecodeError:
                self.protocol_failure = True
                snippet = self.redact_secrets(stripped[:200])
                self.record_event(
                    EventType.error,
                    {
                        "error": (
                            "Malformed protocol output on RPC stdout: "
                            "expected JSONL, got a non-JSON line"
                        ),
                        "fatal": True,
                        "raw_line": snippet,
                    },
                )
                return True

        event = parse_rpc_output_line(line, self.investigation_id, self.next_seq())
        if event is None:
            response = parse_rpc_response(line)
            if response is not None and response.get("success") is False:
                err_text = self.redact_secrets(str(response.get("error", "RPC command rejected")))
                self.protocol_failure = True
                self.record_event(
                    EventType.error,
                    {
                        "error": f"RPC command rejected by Prime Agent: {err_text}",
                        "fatal": True,
                    },
                )
                return True
            return False

        if (
            event.type == EventType.investigation_finished
            and self.aborted_run_open
        ):
            # This agent_end closes the ABORTED run (Prime emits it in response
            # to the official abort). It is not the investigation's completion:
            # it never produces an investigation_finished event and never
            # satisfies completion. The recovery agent_start below opens the
            # fresh run scope whose own agent_end counts.
            return False

        self.event_buffer.append(event)

        if event.type == EventType.agent_ready:
            self.is_run_active = True
            # A new agent run begins: close the aborted-run scope and invalidate
            # any stale agent_end so completion after recovery requires the
            # current (recovery) run's own agent_end.
            self.aborted_run_open = False
            self.agent_end_received = False
        elif event.type == EventType.investigation_finished:
            self.agent_end_received = True
            self._maybe_complete_attempt()
        elif event.type == EventType.tool_call:
            # Official tool_execution_start carries toolCallId. Track start time
            # so the watchdog can abort an execution that never ends.
            call_id = event.payload.get("toolCallId")
            if call_id:
                self.active_tools.setdefault(str(call_id), self._now())
        elif event.type == EventType.tool_result:
            # Only tool_execution_end terminates the execution; updates do not.
            call_id = event.payload.get("toolCallId")
            if call_id and event.payload.get("official_event") == "tool_execution_end":
                self.active_tools.pop(str(call_id), None)

        if len(self.event_buffer) >= self.batch_size:
            await self.flush_events(client)

        return False

    async def start_gateway(self) -> None:
        """Start the loopback World Gateway service (or wire an injected app)."""
        app = self._gateway_app
        if app is None:
            app = create_gateway_app(
                gateway_token=self.world_gateway_token,
                context=self._build_gateway_context(),
                event_callback=self._on_gateway_event,
            )
            self._gateway_app = app
        else:
            # Ensure the bridge records authoritative events from any injected app.
            service = getattr(app.state, "service", None)
            if service is not None:
                service.event_callback = self._on_gateway_event

        if not self.start_gateway_server or self._gateway_server is not None:
            return

        config = uvicorn.Config(
            app=app,
            host="127.0.0.1",
            port=self.gateway_port,
            log_level="warning",
        )
        server = uvicorn.Server(config)
        self._gateway_server = server
        self._gateway_task = asyncio.create_task(server.serve())
        await self._wait_for_gateway_ready()

    async def _wait_for_gateway_ready(self) -> None:
        """Wait until the loopback server accepts connections or raise."""
        for _ in range(40):
            try:
                _reader, writer = await asyncio.open_connection("127.0.0.1", self.gateway_port)
            except OSError:
                await asyncio.sleep(0.05)
                continue
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass
            return
        raise RuntimeError(f"World Gateway failed to start on 127.0.0.1:{self.gateway_port}")

    async def stop_gateway(self) -> None:
        """Stop the loopback World Gateway service."""
        if self._gateway_server is not None:
            self._gateway_server.should_exit = True
        if self._gateway_task is not None:
            self._gateway_task.cancel()
            try:
                await self._gateway_task
            except (asyncio.CancelledError, Exception):
                pass
            self._gateway_task = None
        self._gateway_server = None

    async def start_rpc_process(self) -> asyncio.subprocess.Process:
        """Spawn the prime-agent process in RPC mode or use injected factory.

        The real subprocess runs under the deliberately sanitized child
        environment (see ``build_child_env``). IPython and the World Gateway
        extension tools remain enabled: Prime Agent is launched with
        ``--mode rpc --no-session`` and never with ``--no-builtin-tools``.
        """
        if self._process_factory is not None:
            return await self._process_factory()

        return await asyncio.create_subprocess_exec(
            "prime-agent",
            "--mode",
            "rpc",
            "--no-session",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=self.build_child_env(),
        )

    async def send_abort_command(self, proc: asyncio.subprocess.Process) -> None:
        """Send the official Prime Agent v0.8.1 `{"type":"abort"}` RPC command."""
        if proc.stdin and not proc.stdin.is_closing():
            proc.stdin.write(format_abort_command().encode("utf-8"))
            await proc.stdin.drain()

    async def handle_tool_timeout(
        self,
        proc: asyncio.subprocess.Process,
        *,
        reason: str,
    ) -> None:
        """Abort the active Prime operation once and start bounded recovery.

        ``reason`` is ``"tool_timeout"`` (a tracked tool execution exceeded its
        per-tool age) or ``"run_budget"`` (the overall sandbox run cutoff was
        reached, even with no tool active). Any agent_end received before the
        abort is invalidated so completion after recovery requires a valid
        summary plus the corresponding fresh agent_end, never a stale one from
        the aborted run. The recovery deadline is bounded to the outer
        sandbox deadline minus the run reserve so flush and cleanup fit.
        """
        self.tool_timeout_hit = True
        self.agent_end_received = False
        self.aborted_run_open = True
        self.watchdog_reason = reason
        now = self._now()

        if reason == "run_budget":
            overrun_ids = [str(call_id) for call_id in self.active_tools]
        else:
            overrun_ids = [
                str(call_id)
                for call_id, started_at in self.active_tools.items()
                if now - started_at >= self.tool_timeout_s
            ]
        self.active_tools.clear()

        if not self.abort_sent:
            self.abort_sent = True
            await self.send_abort_command(proc)

        if reason == "run_budget":
            warning_text = (
                f"Prime run reached the overall sandbox budget cutoff at "
                f"~{self.overall_cutoff if self.overall_cutoff is not None else now:.0f}s "
                f"(outer {self.sandbox_timeout_s:.0f}s, recovery "
                f"{self.recovery_timeout_s:.0f}s, reserve {self.run_reserve_s:.0f}s); "
                "sent one official RPC abort and started bounded recovery"
            )
        else:
            warning_text = (
                f"Prime tool execution timed out after {self.tool_timeout_s}s; "
                "sent one official RPC abort and started bounded recovery"
            )
        self.record_event(
            EventType.warning,
            {
                "warning": warning_text,
                "tool_call_ids": overrun_ids,
                "recovery_asked": True,
                "reason": reason,
            },
        )

        # One bounded recovery attempt: instruct Prime to use collected evidence
        # and submit the summary without retrying the timed-out operation.
        await self.send_rpc_command(proc, RECOVERY_INSTRUCTION, mode="steer")

        # Bound the recovery deadline by the outer sandbox deadline minus the
        # run reserve so event flush and child cleanup happen before kill.
        deadline = now + self.recovery_timeout_s
        if self.overall_deadline is not None:
            deadline = min(deadline, self.overall_deadline)
        self.recovery_deadline = deadline

    async def tool_timeout_watchdog(
        self,
        proc: asyncio.subprocess.Process,
        stop_loops_event: asyncio.Event,
    ) -> str:
        """Watch for Prime tool executions that never end.

        Aborts when the FIRST of these holds:
        1. a tracked tool execution exceeds its per-tool age (`tool_timeout_s`);
        2. the overall run cutoff (`sandbox_timeout_s - recovery - reserve`) is
           reached while the attempt is still incomplete -- this covers tools
           that start late in the run, when per-tool age alone would not leave
           room for recovery and flush before Modal kills the container.

        Returns "ok" when the attempt winds down normally, or "recovery_expired"
        when the bounded recovery attempt did not reach a valid summary plus a
        fresh agent_end before the recovery deadline.
        """
        while not stop_loops_event.is_set():
            now = self._now()
            if not self.tool_timeout_hit:
                if self.overall_cutoff is not None and now >= self.overall_cutoff:
                    await self.handle_tool_timeout(proc, reason="run_budget")
                elif self.active_tools and any(
                    now - started_at >= self.tool_timeout_s
                    for started_at in self.active_tools.values()
                ):
                    await self.handle_tool_timeout(proc, reason="tool_timeout")
            if self.recovery_deadline is not None and now >= self.recovery_deadline:
                return "recovery_expired"
            try:
                await asyncio.wait_for(
                    stop_loops_event.wait(),
                    timeout=self.watchdog_interval_s,
                )
            except asyncio.TimeoutError:
                pass
        return "ok"

    async def send_rpc_command(
        self,
        proc: asyncio.subprocess.Process,
        body: str,
        mode: Literal["prompt", "steer", "follow_up"] = "prompt",
        *,
        streaming_behavior: StreamingBehavior | None = None,
    ) -> None:
        """Format and write a typed RPC command to prime-agent stdin."""
        if proc.stdin and not proc.stdin.is_closing():
            wire_cmd = format_rpc_command(
                body,
                mode=mode,
                streaming_behavior=streaming_behavior,
            )
            proc.stdin.write(wire_cmd.encode("utf-8"))
            await proc.stdin.drain()

    async def run(self) -> int:
        """Main bridge lifecycle runner. Owns gateway, child, and flush resources."""
        client = self._custom_client or httpx.AsyncClient()
        exit_code = 1

        try:
            # 1. Start loopback World Gateway before Prime Agent starts.
            await self.start_gateway()

            # 2. Emit and flush investigation_started.
            self.record_event(EventType.investigation_started)
            await self.flush_events(client, force_all=True)

            # 3. Supervise Prime Agent with the retry policy.
            while self.crash_count <= self.max_retries:
                self.agent_end_received = False
                self.is_run_active = False
                self.protocol_failure = False
                self.stderr_tail.clear()
                self.active_tools.clear()
                self.tool_timeout_hit = False
                self.abort_sent = False
                self.recovery_deadline = None
                self.watchdog_reason = None
                self.aborted_run_open = False

                proc = await self.start_rpc_process()
                self._child_process = proc

                # Send the task brief as the first prompt command.
                brief = self.task_brief or "# Begin investigation"
                await self.send_rpc_command(proc, brief, mode="prompt")

                stop_loops_event = asyncio.Event()
                completion_event = asyncio.Event()
                self._completion_event = completion_event

                async def stdout_reader() -> None:
                    if not proc.stdout:
                        return
                    while not proc.stdout.at_eof():
                        line_bytes = await proc.stdout.readline()
                        if not line_bytes:
                            break
                        line_str = line_bytes.decode("utf-8", errors="replace")
                        stop = await self.handle_stdout_line(line_str, client)
                        if stop:
                            break

                async def stderr_reader() -> None:
                    if not proc.stderr:
                        return
                    while not proc.stderr.at_eof():
                        err_bytes = await proc.stderr.readline()
                        if not err_bytes:
                            break
                        err_str = err_bytes.decode("utf-8", errors="replace").strip()
                        if err_str:
                            redacted = self.redact_secrets(err_str)
                            self.stderr_tail.append(redacted)
                            if len(self.stderr_tail) > 50:
                                self.stderr_tail.pop(0)

                async def inbox_poller() -> None:
                    while not stop_loops_event.is_set():
                        try:
                            messages = await self.poll_inbox(client)
                            for msg in messages:
                                body = msg.get("body", "")
                                mode = msg.get("mode", "prompt")
                                run_already_active = self.is_run_active
                                effective_mode, _ = self.process_incoming_chat(body, mode)
                                # Relayed prompts during an active run serialize
                                # with streaming_behavior="steer"; the initial
                                # task brief prompt stays unchanged.
                                streaming_behavior: StreamingBehavior | None = (
                                    "steer"
                                    if effective_mode == "prompt" and run_already_active
                                    else None
                                )
                                await self.send_rpc_command(
                                    proc,
                                    body,
                                    mode=effective_mode,
                                    streaming_behavior=streaming_behavior,
                                )
                        except Exception as e:
                            logger.warning("Inbox polling loop error: %s", e)

                        try:
                            await asyncio.wait_for(
                                stop_loops_event.wait(),
                                timeout=self.inbox_poll_interval_s,
                            )
                        except asyncio.TimeoutError:
                            pass

                async def heartbeat_sender() -> None:
                    while not stop_loops_event.is_set():
                        try:
                            await asyncio.wait_for(
                                stop_loops_event.wait(),
                                timeout=self.heartbeat_interval_s,
                            )
                        except asyncio.TimeoutError:
                            self.record_event(EventType.heartbeat)

                async def periodic_flusher() -> None:
                    while not stop_loops_event.is_set():
                        try:
                            await asyncio.wait_for(
                                stop_loops_event.wait(),
                                timeout=self.batch_interval_s,
                            )
                        except asyncio.TimeoutError:
                            await self.flush_events(client)

                reader_task = asyncio.create_task(stdout_reader())
                completion_task = asyncio.create_task(completion_event.wait())
                watchdog_task = asyncio.create_task(
                    self.tool_timeout_watchdog(proc, stop_loops_event)
                )
                stderr_task = asyncio.create_task(stderr_reader())
                inbox_task = asyncio.create_task(inbox_poller())
                heartbeat_task = asyncio.create_task(heartbeat_sender())
                flusher_task = asyncio.create_task(periodic_flusher())
                background_tasks = [
                    stderr_task,
                    inbox_task,
                    heartbeat_task,
                    flusher_task,
                    watchdog_task,
                ]

                # Race stdout EOF against the per-attempt completion signal and
                # the tool-timeout watchdog. A valid summary plus agent_end may
                # arrive while stdout is still open; never block on EOF before
                # testing success.
                done, _race_pending = await asyncio.wait(
                    {reader_task, completion_task, watchdog_task},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if reader_task in done:
                    exc = reader_task.exception()
                    if exc is not None:
                        raise exc
                run_completed = completion_event.is_set()
                watchdog_result = "ok"
                if watchdog_task in done:
                    watchdog_result = str(watchdog_task.result())

                # Wind down the attempt: stop background loops, then close stdin
                # so Prime Agent's input-end shutdown path can exit cleanly, with
                # a short bounded graceful window before cancelling stragglers
                # and terminating the child. No hangs or leaked tasks.
                stop_loops_event.set()
                # The completion signal task is no longer needed once the race
                # is decided; cancel it now so it never delays wind-down.
                completion_task.cancel()
                if proc.stdin is not None and not proc.stdin.is_closing():
                    try:
                        write_eof = getattr(proc.stdin, "write_eof", None)
                        if callable(write_eof):
                            write_eof()
                        else:
                            proc.stdin.close()
                    except Exception:
                        try:
                            proc.stdin.close()
                        except Exception:
                            pass
                _wind_down_done, pending = await asyncio.wait(
                    {reader_task, *background_tasks},
                    timeout=self.shutdown_grace_s,
                )
                for task in pending:
                    task.cancel()
                await asyncio.gather(
                    completion_task,
                    *pending,
                    return_exceptions=True,
                )

                if proc.returncode is None:
                    try:
                        proc.terminate()
                    except ProcessLookupError:
                        pass
                try:
                    proc_exit = await asyncio.wait_for(
                        proc.wait(),
                        timeout=self.child_stop_timeout_s,
                    )
                except asyncio.TimeoutError:
                    proc_exit = proc.returncode if proc.returncode is not None else -1

                # 4. Evaluate completion. Clean success requires BOTH a valid
                #    summary.submit AND an agent_end event. The completion
                #    signal drives the final forced flush; success is returned
                #    only when persistence of the whole buffer succeeds.
                if run_completed:
                    flushed_all = await self.flush_events(client, force_all=True)
                    if flushed_all:
                        exit_code = 0
                    else:
                        self.record_event(
                            EventType.error,
                            {
                                "error": (
                                    "Run completed but events could not be persisted "
                                    "to the control plane"
                                ),
                                "fatal": True,
                            },
                        )
                        exit_code = 1
                    break

                # Bounded recovery expired without a valid summary plus a fresh
                # agent_end: deterministic abort-and-fail, never fake completion.
                if watchdog_result == "recovery_expired":
                    if self.watchdog_reason == "run_budget":
                        expiry_text = (
                            "Prime run budget was exhausted and the run did not "
                            f"recover within {self.recovery_timeout_s}s to a valid "
                            "summary and fresh agent_end; aborting deterministically "
                            "before the sandbox kill deadline"
                        )
                    else:
                        expiry_text = (
                            f"Prime tool execution timed out and did not recover "
                            f"within {self.recovery_timeout_s}s to a valid summary "
                            "and fresh agent_end; aborting run deterministically"
                        )
                    self.record_event(
                        EventType.error,
                        {
                            "error": expiry_text,
                            "fatal": True,
                        },
                    )
                    exit_code = 1
                    break

                # Protocol failures never retry and always fail visibly.
                if self.protocol_failure:
                    logger.error("RPC protocol failure detected; aborting run")
                    exit_code = 1
                    break

                # A crash after a state-changing gateway call must not be replayed.
                if self.has_mutated:
                    tail_diag = "\n".join(self.stderr_tail[-10:])
                    self.record_event(
                        EventType.error,
                        {
                            "error": (
                                f"Process terminated unexpectedly (exit={proc_exit}) after "
                                "environment mutation without valid summary. "
                                f"Diagnostic tail: {tail_diag}"
                            ),
                            "fatal": True,
                            "stderr_tail": self.stderr_tail[-20:],
                        },
                    )
                    exit_code = 1
                    break

                # Crash before any mutation: retry with backoff.
                self.crash_count += 1
                if self.crash_count <= self.max_retries:
                    logger.warning(
                        "Prime agent failed before mutation (exit=%s). Retrying (%s/%s)...",
                        proc_exit,
                        self.crash_count,
                        self.max_retries,
                    )
                    await asyncio.sleep(0.5)
                    continue

                tail_diag = "\n".join(self.stderr_tail[-10:])
                self.record_event(
                    EventType.error,
                    {
                        "error": (
                            "Prime agent crashed repeatedly before mutation "
                            f"(exit={proc_exit}). Diagnostic tail: {tail_diag}"
                        ),
                        "fatal": True,
                        "stderr_tail": self.stderr_tail[-20:],
                    },
                )
                exit_code = 1
                break

            return exit_code

        except Exception as exc:
            self.record_event(
                EventType.error,
                {"error": f"Unhandled bridge exception: {exc}", "fatal": True},
            )
            return 1

        finally:
            # Resource cleanup on all exit paths. Prefer closing stdin and a
            # short bounded graceful wait so Prime Agent's input-end shutdown
            # path can exit cleanly before terminate/wait.
            child = self._child_process
            if child is not None:
                if child.stdin and not child.stdin.is_closing():
                    try:
                        write_eof = getattr(child.stdin, "write_eof", None)
                        if callable(write_eof):
                            write_eof()
                        else:
                            child.stdin.close()
                    except Exception:
                        pass
                if child.returncode is None:
                    try:
                        await asyncio.wait_for(
                            child.wait(),
                            timeout=self.shutdown_grace_s,
                        )
                    except Exception:
                        pass
                    try:
                        if child.returncode is None:
                            child.terminate()
                    except Exception:
                        pass
                    try:
                        await asyncio.wait_for(
                            child.wait(),
                            timeout=self.child_stop_timeout_s,
                        )
                    except Exception:
                        pass

            await self.stop_gateway()
            await self.flush_events(client, force_all=True)

            if not self._custom_client:
                await client.aclose()


def env_world_context() -> dict[str, Any]:
    """Read truthful world/slice/trace metadata from the container environment.

    Only non-secret metadata reaches the World Gateway context; task payloads,
    credentials, and control-plane-only data never pass through here.
    """
    ctx: dict[str, Any] = {}
    for env_key, context_key in WORLD_ENV_TO_CONTEXT.items():
        value = os.environ.get(env_key)
        if value:
            ctx[context_key] = value

    trace_ref_raw = os.environ.get("TRACE_REF")
    if trace_ref_raw:
        try:
            parsed = json.loads(trace_ref_raw)
            if isinstance(parsed, dict):
                ctx["trace"] = parsed
        except json.JSONDecodeError:
            ctx["trace"] = {"raw": trace_ref_raw[:200]}
    return ctx


def main() -> int:
    """CLI entrypoint for in-sandbox bridge."""
    control_plane_url = os.environ.get("CONTROL_PLANE_CALLBACK_URL", "http://localhost:8000")
    bridge_token = os.environ.get("BRIDGE_TOKEN", "")
    investigation_id_str = os.environ.get("INVESTIGATION_ID")
    if not investigation_id_str:
        print("Error: INVESTIGATION_ID environment variable is required", file=sys.stderr)
        return 2
    try:
        investigation_id = UUID(investigation_id_str)
    except ValueError:
        print(
            f"Error: INVESTIGATION_ID must be a valid UUID, got {investigation_id_str!r}",
            file=sys.stderr,
        )
        return 2
    task_brief = os.environ.get("TASK_BRIEF", "")

    try:
        tool_timeout_s = float(os.environ.get("TOOL_TIMEOUT_S", DEFAULT_TOOL_TIMEOUT_S))
        recovery_timeout_s = float(
            os.environ.get("TOOL_RECOVERY_TIMEOUT_S", DEFAULT_TOOL_RECOVERY_TIMEOUT_S)
        )
    except ValueError:
        print(
            "Error: TOOL_TIMEOUT_S and TOOL_RECOVERY_TIMEOUT_S must be numeric",
            file=sys.stderr,
        )
        return 2

    sandbox_timeout_raw = os.environ.get("SANDBOX_TIMEOUT_S", "")
    sandbox_timeout_s: float | None = None
    if sandbox_timeout_raw:
        try:
            sandbox_timeout_s = float(sandbox_timeout_raw)
        except ValueError:
            print(
                "Error: SANDBOX_TIMEOUT_S must be numeric",
                file=sys.stderr,
            )
            return 2
        if sandbox_timeout_s <= 0:
            sandbox_timeout_s = None

    bridge = SandboxBridge(
        control_plane_url=control_plane_url,
        bridge_token=bridge_token,
        investigation_id=investigation_id,
        task_brief=task_brief,
        world_context=env_world_context(),
        tool_timeout_s=tool_timeout_s,
        recovery_timeout_s=recovery_timeout_s,
        sandbox_timeout_s=sandbox_timeout_s,
    )
    return asyncio.run(bridge.run())


if __name__ == "__main__":
    sys.exit(main())
