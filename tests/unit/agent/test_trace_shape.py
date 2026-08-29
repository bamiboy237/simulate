"""This module checks trace shape after sanitization for checkpoint 2.4.

This module checks the allowlist.
This module checks disabled mode.
"""

import pytest
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode

from app.telemetry.allowlist import sanitize_attributes
from app.telemetry.recorder import TraceRecorder


def test_allowed_attributes_are_recorded(
    span_capture: tuple[TraceRecorder, InMemorySpanExporter],
) -> None:
    recorder, exporter = span_capture
    with recorder.span("support_agent.turn") as span:
        span.set_attribute("support.intent", "order_status")
        span.set_attribute("support.confidence", 0.9)
        span.set_attribute("support.message.length", 42)
        span.set_attribute("support.policy.grounded", True)
        span.set_attribute("prompt.version", "1")

    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    assert spans[0].name == "support_agent.turn"
    assert spans[0].attributes == {
        "support.intent": "order_status",
        "support.confidence": 0.9,
        "support.message.length": 42,
        "support.policy.grounded": True,
        "prompt.version": "1",
        "langsmith.metadata.support.intent": "order_status",
        "langsmith.metadata.support.confidence": 0.9,
        "langsmith.metadata.support.message.length": 42,
        "langsmith.metadata.support.policy.grounded": True,
        "langsmith.metadata.prompt.version": "1",
    }


def test_non_allowlisted_attributes_are_dropped(
    span_capture: tuple[TraceRecorder, InMemorySpanExporter],
) -> None:
    recorder, exporter = span_capture
    with recorder.span("support_agent.turn") as span:
        span.set_attribute("secret_api_key", "sk-live-123")
        span.set_attribute("user.message", "unrestricted customer text")
        span.set_attribute("order.total_amount", "48.25")
        span.set_attribute("support.intent", "policy")

    spans = exporter.get_finished_spans()
    assert spans[0].attributes == {
        "support.intent": "policy",
        "langsmith.metadata.support.intent": "policy",
    }


def test_forbidden_substrings_are_dropped_from_allowed_keys(
    span_capture: tuple[TraceRecorder, InMemorySpanExporter],
) -> None:
    recorder, exporter = span_capture
    with recorder.span("support_agent.turn") as span:
        span.set_attribute("model.run.id", "run-with-test-secret-key-inside")

    spans = exporter.get_finished_spans()
    assert spans[0].attributes == {}


def test_long_attribute_values_are_truncated(
    span_capture: tuple[TraceRecorder, InMemorySpanExporter],
) -> None:
    recorder, exporter = span_capture
    with recorder.span("support_agent.turn") as span:
        span.set_attribute("support.reason.code", "x" * 500)

    spans = exporter.get_finished_spans()
    assert len(spans[0].attributes["support.reason.code"]) == 256


def test_span_tree_parent_child_relationships(
    span_capture: tuple[TraceRecorder, InMemorySpanExporter],
) -> None:
    recorder, exporter = span_capture
    with recorder.span("support_agent.turn") as turn_span:
        turn_context = turn_span.span_context()
        assert turn_context is not None
        with recorder.span("support_agent.routing"):
            pass
        with recorder.span("support_agent.answer") as answer_span:
            answer_context = answer_span.span_context()
            assert answer_context is not None
            with recorder.span("support_agent.tool.get_order_status") as tool_span:
                tool_context = tool_span.span_context()
                assert tool_context is not None
                with recorder.span("support_agent.database.read"):
                    pass

    spans = {span.name: span for span in exporter.get_finished_spans()}
    routing_parent = spans["support_agent.routing"].parent
    assert routing_parent is not None
    assert routing_parent.span_id == turn_context.span_id

    answer_parent = spans["support_agent.answer"].parent
    assert answer_parent is not None
    assert answer_parent.span_id == turn_context.span_id

    tool_parent = spans["support_agent.tool.get_order_status"].parent
    assert tool_parent is not None
    assert tool_parent.span_id == answer_context.span_id

    db_parent = spans["support_agent.database.read"].parent
    assert db_parent is not None
    assert db_parent.span_id == tool_context.span_id


def test_escaping_exceptions_leave_no_trace_events(
    span_capture: tuple[TraceRecorder, InMemorySpanExporter],
) -> None:
    """This test checks that the recorder produces no events and leaks no details
    when an exception escapes a span.
    """
    recorder, exporter = span_capture
    with pytest.raises(RuntimeError, match="connection refused"):
        with recorder.span("support_agent.tool.get_order_status"):
            raise RuntimeError("connection refused for secret-endpoint")

    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    assert spans[0].events == ()
    serialized = str(spans[0].status) + str(spans[0].attributes)
    assert "secret-endpoint" not in serialized
    assert "connection refused" not in serialized


def test_error_status_uses_reason_code(
    span_capture: tuple[TraceRecorder, InMemorySpanExporter],
) -> None:
    recorder, exporter = span_capture
    with recorder.span("support_agent.tool.get_order_status") as span:
        span.set_error("order_not_found")

    spans = exporter.get_finished_spans()
    assert spans[0].status.status_code is StatusCode.ERROR
    assert spans[0].status.description == "order_not_found"


def test_disabled_recorder_performs_no_telemetry_work() -> None:
    recorder = TraceRecorder(None)

    assert recorder.enabled is False
    assert recorder.current_trace_id() is None

    with recorder.span("support_agent.turn") as span:
        span.set_attribute("support.intent", "order_status")
        span.set_error("order_not_found")

    # No tracer exists, so nothing can have been exported or recorded.
    assert recorder.current_trace_id() is None


def test_trace_id_is_hex_when_tracing_is_enabled(
    span_capture: tuple[TraceRecorder, InMemorySpanExporter],
) -> None:
    recorder, exporter = span_capture
    with recorder.span("support_agent.turn"):
        trace_id = recorder.current_trace_id()

    assert trace_id is not None
    assert len(trace_id) == 32
    int(trace_id, 16)  # valid hex
    assert len(exporter.get_finished_spans()) == 1


def test_sanitize_attributes_unit() -> None:
    cleaned = sanitize_attributes(
        {
            "support.intent": "refund",
            "support.nested.value": {"a": 1},
            "support.list.value": [1, 2],
            "support.reason.code": "",
            "support.confidence": None,
            "not.allowed": "value",
        }
    )
    assert cleaned == {"support.intent": "refund"}

    cleaned = sanitize_attributes(
        {"support.reason.code": "contains-secret-key-value"},
        forbidden_substrings=("secret-key",),
    )
    assert cleaned == {}
