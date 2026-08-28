"""Unit tests for Prime Agent RPC command formatting, response parsing, and event mapping."""

import json
from uuid import uuid4

from app.domain.agent_runner.prime_rpc import (
    format_abort_command,
    format_rpc_command,
    parse_rpc_output_line,
    parse_rpc_response,
)
from app.domain.runner.schemas import EventType


def test_format_abort_command_matches_official_json_shape() -> None:
    """Verify the official v0.8.1 abort command has no extraneous fields."""
    raw = format_abort_command()
    assert raw.endswith("\n")
    data = json.loads(raw.strip())
    assert data == {"type": "abort"}


def test_format_rpc_commands_match_official_json_shape() -> None:
    """Verify RPC commands format with typed 'type' and 'message' fields."""
    # Prompt mode
    prompt_cmd = format_rpc_command("Analyze checkout error", mode="prompt")
    assert prompt_cmd.endswith("\n")
    data_prompt = json.loads(prompt_cmd.strip())
    assert data_prompt["type"] == "prompt"
    assert data_prompt["message"] == "Analyze checkout error"
    assert "content" not in data_prompt

    # Steer mode
    steer_cmd = format_rpc_command("Focus on payment logs", mode="steer")
    assert steer_cmd.endswith("\n")
    data_steer = json.loads(steer_cmd.strip())
    assert data_steer["type"] == "steer"
    assert data_steer["message"] == "Focus on payment logs"

    # Follow-up mode
    follow_up_cmd = format_rpc_command("What about order #42?", mode="follow_up")
    assert follow_up_cmd.endswith("\n")
    data_follow_up = json.loads(follow_up_cmd.strip())
    assert data_follow_up["type"] == "follow_up"
    assert data_follow_up["message"] == "What about order #42?"

    # Default mode is prompt
    default_cmd = format_rpc_command("Default task")
    data_default = json.loads(default_cmd.strip())
    assert data_default["type"] == "prompt"
    assert data_default["message"] == "Default task"


def test_official_rpc_responses_do_not_become_false_domain_events() -> None:
    """Verify official documented RPC response lines do not map to domain events."""
    inv_id = uuid4()

    response_lines = [
        '{"type": "response", "command": "prompt", "success": true}',
        '{"type": "response", "id": "req-1", "success": false, "error": "busy"}',
        '{"type": "response", "success": true, "result": {"session": "abc"}}',
    ]

    for line in response_lines:
        # parse_rpc_response detects it as a response
        parsed = parse_rpc_response(line)
        assert parsed is not None

        # parse_rpc_output_line ignores it and returns None
        event = parse_rpc_output_line(line, inv_id, 1)
        assert event is None, f"Response line '{line}' was incorrectly treated as a domain event!"


def test_official_event_mappings_and_raw_event_preservation() -> None:
    """Verify official nested streaming and camelCase tool events map to domain events."""
    inv_id = uuid4()

    # 1. agent_start -> agent_ready (no agentId payload in official protocol)
    start_line = '{"type": "agent_start"}'
    e_start = parse_rpc_output_line(start_line, inv_id, 1)
    assert e_start is not None
    assert e_start.type == EventType.agent_ready
    assert e_start.payload["raw_event"] == "agent_start"
    assert "agentId" not in e_start.payload

    # 2. agent_end -> investigation_finished (carries messages, never summary_submitted)
    end_line = '{"type": "agent_end", "messages": []}'
    e_end = parse_rpc_output_line(end_line, inv_id, 2)
    assert e_end is not None
    assert e_end.type == EventType.investigation_finished
    assert e_end.type != EventType.summary_submitted
    assert e_end.payload["raw_event"] == "agent_end"
    assert e_end.payload["messages"] == []
    assert "exitCode" not in e_end.payload

    # 3. tool_execution_start -> tool_call
    tool_start = (
        '{"type": "tool_execution_start", "toolCallId": "tc_1", '
        '"toolName": "trace.read", "args": {"id": "t1"}}'
    )
    e_tool_start = parse_rpc_output_line(tool_start, inv_id, 3)
    assert e_tool_start is not None
    assert e_tool_start.type == EventType.tool_call
    assert e_tool_start.payload["toolName"] == "trace.read"
    assert e_tool_start.payload["toolCallId"] == "tc_1"
    assert e_tool_start.payload["raw_event"] == "tool_execution_start"

    # 4. tool_execution_update -> tool_result
    tool_update = (
        '{"type": "tool_execution_update", "toolCallId": "tc_1", '
        '"toolName": "trace.read", "partialResult": "reading chunks..."}'
    )
    e_tool_update = parse_rpc_output_line(tool_update, inv_id, 4)
    assert e_tool_update is not None
    assert e_tool_update.type == EventType.tool_result
    assert e_tool_update.payload["partialResult"] == "reading chunks..."
    assert e_tool_update.payload["toolCallId"] == "tc_1"
    assert e_tool_update.payload["raw_event"] == "tool_execution_update"

    # 5. tool_execution_end -> tool_result
    tool_end = (
        '{"type": "tool_execution_end", "toolCallId": "tc_1", '
        '"toolName": "trace.read", "result": {"trace": "data"}, "isError": false}'
    )
    e_tool_end = parse_rpc_output_line(tool_end, inv_id, 5)
    assert e_tool_end is not None
    assert e_tool_end.type == EventType.tool_result
    assert e_tool_end.payload["result"] == {"trace": "data"}
    assert e_tool_end.payload["toolCallId"] == "tc_1"
    assert e_tool_end.payload["isError"] is False
    assert e_tool_end.payload["raw_event"] == "tool_execution_end"

    # 6. Nested text_delta in message_update with message field -> message
    msg_line = (
        '{"type": "message_update", "message": {"role": "assistant", "content": []}, '
        '"assistantMessageEvent": {"type": "text_delta", "delta": "Analyzing trace details..."}}'
    )
    e_msg = parse_rpc_output_line(msg_line, inv_id, 6)
    assert e_msg is not None
    assert e_msg.type == EventType.message
    assert e_msg.payload["message"]["role"] == "assistant"
    assert e_msg.payload["assistantMessageEvent"]["delta"] == "Analyzing trace details..."
    assert e_msg.payload["raw_event"] == "text_delta"

    # 7. Nested thinking_delta in message_update with message field -> thought
    thought_line = (
        '{"type": "message_update", "message": {"role": "assistant", "content": []}, '
        '"assistantMessageEvent": {"type": "thinking_delta", "delta": "Thinking..."}}'
    )
    e_thought = parse_rpc_output_line(thought_line, inv_id, 7)
    assert e_thought is not None
    assert e_thought.type == EventType.thought
    assert e_thought.payload["message"]["role"] == "assistant"
    assert e_thought.payload["assistantMessageEvent"]["delta"] == "Thinking..."
    assert e_thought.payload["raw_event"] == "thinking_delta"

    # 8. Nested error in message_update with message field -> error
    error_line = (
        '{"type": "message_update", "message": {"role": "assistant", "content": []}, '
        '"assistantMessageEvent": {"type": "error", "error": "Model context limit exceeded"}}'
    )
    e_err = parse_rpc_output_line(error_line, inv_id, 8)
    assert e_err is not None
    assert e_err.type == EventType.error
    assert e_err.payload["message"]["role"] == "assistant"
    assert e_err.payload["assistantMessageEvent"]["error"] == "Model context limit exceeded"
    assert e_err.payload["raw_event"] == "error"


def test_parse_rpc_output_line_unstructured_or_empty() -> None:
    """Verify empty lines return None and plain text becomes message events with raw_event."""
    inv_id = uuid4()

    # Empty
    assert parse_rpc_output_line("", inv_id, 1) is None
    assert parse_rpc_output_line("   \n", inv_id, 1) is None

    # Plain text
    plain_event = parse_rpc_output_line("Booting container harness...", inv_id, 2)
    assert plain_event is not None
    assert plain_event.type == EventType.message
    assert plain_event.payload["text"] == "Booting container harness..."
    assert plain_event.payload["raw_event"] == "stdout"
