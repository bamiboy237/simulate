"""Unit tests for Prime Agent RPC command formatting and output line parsing."""

import json
from uuid import uuid4

from app.domain.agent_runner.prime_rpc import format_rpc_command, parse_rpc_output_line
from app.domain.runner.schemas import EventType


def test_format_rpc_command() -> None:
    """Verify prompt, steer, and follow_up modes format to newline-delimited JSON."""
    prompt_cmd = format_rpc_command("prompt", "Analyze checkout error")
    assert prompt_cmd.endswith("\n")
    data = json.loads(prompt_cmd.strip())
    assert data["command"] == "prompt"
    assert data["content"] == "Analyze checkout error"

    steer_cmd = format_rpc_command("steer", "Focus on order table")
    steer_data = json.loads(steer_cmd.strip())
    assert steer_data["command"] == "steer"

    follow_up_cmd = format_rpc_command("follow_up", "What was the final status?")
    follow_up_data = json.loads(follow_up_cmd.strip())
    assert follow_up_data["command"] == "follow_up"


def test_parse_rpc_output_line_json_events() -> None:
    """Verify JSON output lines map to typed RunnerEvents."""
    inv_id = uuid4()

    # Thought
    thought_line = '{"type": "thought", "payload": {"thought": "Reading trace"}}'
    event = parse_rpc_output_line(thought_line, inv_id, 1)
    assert event is not None
    assert event.investigation_id == inv_id
    assert event.seq == 1
    assert event.type == EventType.thought
    assert event.payload["thought"] == "Reading trace"

    # Tool call
    tool_line = '{"type": "tool_call", "payload": {"tool": "trace.read"}}'
    tool_event = parse_rpc_output_line(tool_line, inv_id, 2)
    assert tool_event is not None
    assert tool_event.type == EventType.tool_call
    assert tool_event.payload["tool"] == "trace.read"

    # Summary submitted
    summary_line = (
        '{"type": "summary_submitted", "payload": '
        '{"findings": "root cause found", "next_step": "fix"}}'
    )
    summary_event = parse_rpc_output_line(summary_line, inv_id, 3)
    assert summary_event is not None
    assert summary_event.type == EventType.summary_submitted
    assert summary_event.payload["findings"] == "root cause found"


def test_parse_rpc_output_line_unstructured_or_empty() -> None:
    """Verify empty lines return None and plain text becomes message events."""
    inv_id = uuid4()

    # Empty
    assert parse_rpc_output_line("", inv_id, 1) is None
    assert parse_rpc_output_line("   \n", inv_id, 1) is None

    # Plain text
    plain_event = parse_rpc_output_line("Booting container harness...", inv_id, 2)
    assert plain_event is not None
    assert plain_event.type == EventType.message
    assert plain_event.payload["text"] == "Booting container harness..."
