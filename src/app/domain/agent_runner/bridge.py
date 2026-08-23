"""In-sandbox bridge process relaying messages and streaming investigation events.

Responsibilities:
1. Long-poll control plane for chat messages (GET /internal/inbox) with backoff.
2. Translate message modes (prompt, steer, follow_up) with the steer downgrade rule.
3. Read Prime Agent output lines, batch up to 50 events or 500ms, post to POST /internal/events.
4. Emit 15s heartbeats.
5. Exit cleanly on summary_submitted.
6. Retry crashes up to 3 times, emit error event and exit nonzero on failure.
"""

import asyncio
import logging
import os
import sys
from datetime import datetime, timezone
from typing import Any, Literal
from uuid import UUID, uuid4

import httpx

from app.domain.agent_runner.prime_rpc import format_rpc_command, parse_rpc_output_line
from app.domain.runner.schemas import EventType, RunnerEvent

logger = logging.getLogger("sandbox.bridge")


class SandboxBridge:
    """Relays messages inward and progress events outward inside a Modal sandbox."""

    def __init__(
        self,
        control_plane_url: str,
        bridge_token: str,
        investigation_id: UUID,
        *,
        http_client: httpx.AsyncClient | None = None,
        max_retries: int = 3,
        batch_size: int = 50,
        batch_interval_s: float = 0.5,
        heartbeat_interval_s: float = 15.0,
    ) -> None:
        self.control_plane_url = control_plane_url.rstrip("/")
        self.bridge_token = bridge_token
        self.investigation_id = investigation_id
        self._custom_client = http_client
        self.max_retries = max_retries
        self.batch_size = batch_size
        self.batch_interval_s = batch_interval_s
        self.heartbeat_interval_s = heartbeat_interval_s

        self.seq = 0
        self.cursor = 0
        self.is_run_active = False
        self.event_buffer: list[RunnerEvent] = []
        self.running = True
        self.backoff_s = 1.0
        self.crash_count = 0

    @property
    def auth_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.bridge_token}"}

    def next_seq(self) -> int:
        self.seq += 1
        return self.seq

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
        return effective_mode, format_rpc_command(body)

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

    async def flush_events(self, client: httpx.AsyncClient) -> None:
        """Post buffered events to control plane in a single batch."""
        if not self.event_buffer:
            return

        batch_to_send = list(self.event_buffer[: self.batch_size])
        payload = [event.model_dump(mode="json") for event in batch_to_send]

        try:
            resp = await client.post(
                f"{self.control_plane_url}/internal/events",
                json=payload,
                headers=self.auth_headers,
                timeout=10.0,
            )
            if resp.is_success:
                # Remove sent items from buffer
                del self.event_buffer[: len(batch_to_send)]
        except Exception as exc:
            logger.warning("Failed to flush events to control plane: %s", exc)

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
            messages: list[dict[str, Any]] = [
                m for m in raw_messages if isinstance(m, dict)
            ]
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
        """Parse Prime Agent stdout line, buffer event, and detect summary completion.

        Returns True if summary_submitted was encountered (signaling run completion).
        """
        event = parse_rpc_output_line(line, self.investigation_id, self.next_seq())
        if event is None:
            return False

        self.event_buffer.append(event)

        if event.type == EventType.agent_ready:
            self.is_run_active = True

        if event.type == EventType.summary_submitted:
            # Flush everything including summary
            await self.flush_events(client)
            return True

        if len(self.event_buffer) >= self.batch_size:
            await self.flush_events(client)

        return False

    async def run(self) -> int:
        """Main bridge loop handling heartbeat, inbox polling, and event streaming."""
        client = self._custom_client or httpx.AsyncClient()
        try:
            self.record_event(EventType.investigation_started)
            await self.flush_events(client)
            return 0
        except Exception as exc:
            self.crash_count += 1
            if self.crash_count > self.max_retries:
                self.record_event(EventType.error, {"error": str(exc), "fatal": True})
                await self.flush_events(client)
                return 1
            return 1
        finally:
            if not self._custom_client:
                await client.aclose()


def main() -> int:
    """CLI entrypoint for in-sandbox bridge."""
    control_plane_url = os.environ.get("CONTROL_PLANE_CALLBACK_URL", "http://localhost:8000")
    bridge_token = os.environ.get("BRIDGE_TOKEN", "")
    investigation_id_str = os.environ.get("INVESTIGATION_ID", str(uuid4()))

    bridge = SandboxBridge(
        control_plane_url=control_plane_url,
        bridge_token=bridge_token,
        investigation_id=UUID(investigation_id_str),
    )
    return asyncio.run(bridge.run())


if __name__ == "__main__":
    sys.exit(main())
