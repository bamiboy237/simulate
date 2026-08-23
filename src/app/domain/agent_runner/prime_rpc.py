"""Prime Agent RPC protocol adapter.

Encapsulates Prime Agent installation, line-based RPC command serialization,
and stdout line parsing into typed RunnerEvents.
"""

import json
from typing import Any, Literal
from uuid import UUID

from app.domain.runner.schemas import EventType, RunnerEvent


def format_rpc_command(mode: Literal["prompt", "steer", "follow_up"], body: str) -> str:
    """Format a user chat message into a line-delimited Prime Agent RPC command."""
    payload = {
        "command": mode,
        "content": body,
    }
    return json.dumps(payload, separators=(",", ":")) + "\n"


def parse_rpc_output_line(
    raw_line: str,
    investigation_id: UUID,
    seq: int,
) -> RunnerEvent | None:
    """Parse a single stdout JSON line from Prime Agent into a RunnerEvent.

    Returns None if the line is empty or not JSON.
    """
    line = raw_line.strip()
    if not line:
        return None

    try:
        data = json.loads(line)
    except json.JSONDecodeError:
        # Non-JSON stdout lines are mapped to standard messages
        return RunnerEvent(
            investigation_id=investigation_id,
            seq=seq,
            type=EventType.message,
            payload={"text": line},
        )

    if not isinstance(data, dict):
        return RunnerEvent(
            investigation_id=investigation_id,
            seq=seq,
            type=EventType.message,
            payload={"text": line},
        )

    event_type_str = str(data.get("type", data.get("event", "thought"))).lower()
    try:
        event_type = EventType(event_type_str)
    except ValueError:
        # Map unknown event types to thought or message
        event_type = EventType.thought

    # Extract payload
    payload: dict[str, Any] = data.get("payload", data)

    return RunnerEvent(
        investigation_id=investigation_id,
        seq=seq,
        type=event_type,
        payload=payload,
    )
