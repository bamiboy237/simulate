"""Prime Agent v0.8.1 RPC protocol adapter.

Encapsulates Prime Agent line-based RPC command serialization, response handling,
and stdout line parsing into typed RunnerEvents according to Prime Agent v0.8.1.
"""

import json
from typing import Any, Literal
from uuid import UUID

from app.domain.runner.schemas import EventType, RunnerEvent

RpcMode = Literal["prompt", "steer", "follow_up"]
StreamingBehavior = Literal["steer", "followUp"]

# Official Prime Agent v0.8.1 event types needed by this milestone, mapped to
# domain event types. Potential unknown event types fall back to domain
# EventType values, so no speculative Prime RPC aliases belong here.
OFFICIAL_EVENT_MAPPING: dict[str, EventType] = {
    "agent_start": EventType.agent_ready,
    "agent_end": EventType.investigation_finished,
    "message_update": EventType.message,
    "tool_execution_start": EventType.tool_call,
    "tool_execution_update": EventType.tool_result,
    "tool_execution_end": EventType.tool_result,
    "error": EventType.error,
}


def format_abort_command() -> str:
    """Format the official Prime Agent v0.8.1 abort command.

    Line-delimited `{"type": "abort"}` cancels the in-flight tool execution or
    generation without ending the RPC session, so the run can continue after a
    bounded recovery instruction.
    """
    return json.dumps({"type": "abort"}, separators=(",", ":")) + "\n"


def format_rpc_command(
    body: str,
    mode: RpcMode = "prompt",
    *,
    streaming_behavior: StreamingBehavior | None = None,
) -> str:
    """Format a user chat message into a line-delimited Prime Agent v0.8.1 RPC command.

    Emits typed JSON objects on the wire with the official 'message' field:
    - {"type": "prompt", "message": "..."}
    - {"type": "steer", "message": "..."}
    - {"type": "follow_up", "message": "..."}
    """
    payload: dict[str, Any] = {
        "type": mode,
        "message": body,
    }
    if streaming_behavior is not None and mode == "prompt":
        payload["streamingBehavior"] = streaming_behavior

    return json.dumps(payload, separators=(",", ":")) + "\n"


def parse_rpc_response(raw_line: str) -> dict[str, Any] | None:
    """Parse an official RPC command response line from stdout.

    Returns the parsed dict if the line is a documented RPC response (type == "response"),
    or None if it is an event or invalid JSON.
    """
    line = raw_line.strip()
    if not line:
        return None

    try:
        data = json.loads(line)
    except json.JSONDecodeError:
        return None

    if isinstance(data, dict) and data.get("type") == "response":
        return data

    return None


def parse_rpc_output_line(
    raw_line: str,
    investigation_id: UUID,
    seq: int,
) -> RunnerEvent | None:
    """Parse a single stdout JSON line from Prime Agent into a typed RunnerEvent.

    Official command responses (type == "response") return None to avoid
    producing false domain events.

    Official events are mapped to domain event types:
    - agent_start -> agent_ready
    - message_update with assistantMessageEvent.type == text_delta -> message
    - message_update with assistantMessageEvent.type == thinking_delta -> thought
    - tool_execution_start -> tool_call
    - tool_execution_update/end -> tool_result
    - message_update with assistantMessageEvent.type == error -> error
    - agent_end -> investigation_finished (never summary_submitted!)

    The original official event type name is preserved in payload['raw_event']
    and payload['official_event'] for diagnosis.
    """
    line = raw_line.strip()
    if not line:
        return None

    try:
        data = json.loads(line)
    except json.JSONDecodeError:
        # Non-JSON stdout lines are mapped to standard message events
        return RunnerEvent(
            investigation_id=investigation_id,
            seq=seq,
            type=EventType.message,
            payload={"text": line, "raw_event": "stdout", "official_event": "stdout"},
        )

    if not isinstance(data, dict):
        return RunnerEvent(
            investigation_id=investigation_id,
            seq=seq,
            type=EventType.message,
            payload={"text": line, "raw_event": "stdout", "official_event": "stdout"},
        )

    # Filter out official RPC response lines
    if parse_rpc_response(line) is not None:
        return None

    raw_type_str = str(data.get("type", data.get("event", "thought"))).lower()

    # Handle official nested streaming event shape:
    # {"type": "message_update", "assistantMessageEvent": {"type": "text_delta" ...}}
    assistant_event = data.get("assistantMessageEvent")
    if isinstance(assistant_event, dict):
        nested_type = str(assistant_event.get("type", "")).lower()
        if nested_type == "thinking_delta":
            event_type = EventType.thought
            raw_type_str = "thinking_delta"
        elif nested_type == "text_delta":
            event_type = EventType.message
            raw_type_str = "text_delta"
        elif nested_type in OFFICIAL_EVENT_MAPPING:
            event_type = OFFICIAL_EVENT_MAPPING[nested_type]
            raw_type_str = nested_type
        else:
            event_type = EventType.message
            raw_type_str = nested_type or raw_type_str
    elif raw_type_str in OFFICIAL_EVENT_MAPPING:
        event_type = OFFICIAL_EVENT_MAPPING[raw_type_str]
    else:
        try:
            event_type = EventType(raw_type_str)
        except ValueError:
            event_type = EventType.thought

    # Build payload preserving original official event data
    if "payload" in data and isinstance(data["payload"], dict):
        payload: dict[str, Any] = dict(data["payload"])
    elif "data" in data and isinstance(data["data"], dict):
        payload = dict(data["data"])
    else:
        payload = dict(data)

    payload["raw_event"] = raw_type_str
    payload["official_event"] = raw_type_str

    return RunnerEvent(
        investigation_id=investigation_id,
        seq=seq,
        type=event_type,
        payload=payload,
    )
