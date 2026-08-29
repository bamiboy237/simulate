"""Focused tests for the generic event stream contract."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.domain.user_simulator.events import (
    CompositeSink,
    DisplayEvent,
    DisplayMemory,
    EventEmitter,
    EventFactory,
    EventKind,
    EventSink,
    EventSource,
    JsonlPersistentSink,
    NonFatalSink,
    PersistentEvent,
    SimulationEvent,
)


def test_display_event_is_deliberately_blocked_from_serialization() -> None:
    from datetime import UTC, datetime

    event = DisplayEvent(
        kind=EventKind.USER,
        source=EventSource.PERSONA,
        seq=1,
        timestamp=datetime.now(UTC),
        text="secret chat text",
    )
    with pytest.raises(TypeError, match="display-only"):
        event.to_json()
    with pytest.raises(TypeError, match="display-only"):
        event.model_dump()
    with pytest.raises(TypeError):
        json.dumps(event)


def test_persistent_event_serializes_only_allowlisted_scalar_fields() -> None:
    from datetime import UTC, datetime

    event = PersistentEvent(
        kind=EventKind.TOOL_RESULT,
        source=EventSource.REFERENCE,
        seq=4,
        run_id="r1",
        case_id="c1",
        timestamp=datetime.now(UTC),
        fields=(
            ("tool", "get_order"),
            ("outcome", "ok"),
            ("message", "secret chat text"),
            ("nested", {"a": 1}),
            ("tokens", 12),
        ),
    )
    payload = json.loads(event.to_json())
    assert payload["tool"] == "get_order"
    assert payload["outcome"] == "ok"
    assert payload["tokens"] == 12
    assert "message" not in payload  # chat is never a persistent field
    assert "nested" not in payload  # non-scalar values are dropped
    assert payload["event"] == "tool_result"
    assert payload["seq"] == 4


def test_event_factory_sequences_events_and_carries_run_identity() -> None:
    factory = EventFactory("run-1", "case-1")
    first = factory.build(
        EventKind.START, EventSource.ENGINE, text="start", detail=("a", "b")
    )
    second = factory.build(EventKind.USER, EventSource.PERSONA, text="hi", turn=1)
    assert first.persistent.seq == 1
    assert second.persistent.seq == 2
    assert first.display is not None and first.display.detail == ("a", "b")
    assert second.persistent.run_id == "run-1"
    assert second.persistent.case_id == "case-1"
    assert second.display is not None and second.display.text == "hi"
    assert second.persistent.fields == (("turn", 1),)


def test_event_emitter_fans_out_to_every_sink() -> None:
    class _Recorder:
        def __init__(self) -> None:
            self.events: list[SimulationEvent] = []

        def emit(self, event: SimulationEvent) -> None:
            self.events.append(event)

    left, right = _Recorder(), _Recorder()
    emitter = EventEmitter("r", "c", (left, right))
    emitter.emit(EventKind.START, EventSource.ENGINE, text="go")
    assert len(left.events) == 1
    assert len(right.events) == 1
    assert left.events[0].persistent.seq == 1


def test_composite_sink_add_and_fan_out() -> None:
    class _Recorder(EventSink):
        def __init__(self) -> None:
            self.count = 0

        def emit(self, event: SimulationEvent) -> None:
            del event
            self.count += 1

    composite = CompositeSink()
    assert len(composite) == 0
    first, second = _Recorder(), _Recorder()
    composite.add(first)
    composite.add(second)
    event = EventFactory("r", "c").build(EventKind.DONE, EventSource.ENGINE)
    composite.emit(event)
    assert first.count == 1
    assert second.count == 1


def test_jsonl_sink_writes_only_persistent_fields_and_never_chat(
    tmp_path: Path,
) -> None:
    sink = JsonlPersistentSink("run-1", "case-1", tmp_path)
    emitter = EventEmitter("run-1", "case-1", (sink,))
    emitter.emit(
        EventKind.USER,
        EventSource.PERSONA,
        text="actual chat that must never be persisted",
        turn=1,
    )
    emitter.emit(
        EventKind.TOOL_RESULT,
        EventSource.SUPPORT,
        text="get_order_status(order_id=abc) ok",
        tool="get_order_status",
        outcome="ok",
    )
    lines = (tmp_path / "run-1.jsonl").read_text().splitlines()
    assert len(lines) == 2
    joined = "\n".join(lines)
    assert "actual chat" not in joined
    assert "get_order_status" in joined
    assert "abc" not in joined  # tool argument values are not persistent fields
    first = json.loads(lines[0])
    assert first["event"] == "user"
    assert first["turn"] == 1


def test_jsonl_sink_creates_the_file_lazily_on_first_emit(tmp_path: Path) -> None:
    sink = JsonlPersistentSink("lazy", "case", tmp_path)
    assert not (tmp_path / "lazy.jsonl").exists()
    sink.emit(EventFactory("lazy", "case").build(EventKind.START, EventSource.ENGINE))
    assert (tmp_path / "lazy.jsonl").exists()


def test_display_memory_keeps_chat_in_memory_and_never_serializes() -> None:
    memory = DisplayMemory()
    emitter = EventEmitter("r", "c", (memory,))
    emitter.emit(EventKind.USER, EventSource.PERSONA, text="hello there", turn=1)
    emitter.emit(EventKind.AGENT, EventSource.SUPPORT, text="hi", turn=1)
    emitter.emit(EventKind.MODEL, EventSource.SUPPORT, text="model call", tokens=5)
    assert len(memory) == 3
    chat = memory.chat
    assert [event.kind for event in chat] == [EventKind.USER, EventKind.AGENT]
    assert chat[0].text == "hello there"
    with pytest.raises(TypeError):
        memory.chat[0].to_json()


def test_display_event_repr_redacts_chat_text() -> None:
    from datetime import UTC, datetime

    event = DisplayEvent(
        kind=EventKind.USER,
        source=EventSource.PERSONA,
        seq=1,
        timestamp=datetime.now(UTC),
        text="secret chat line",
        detail=("run_id=r", "jsonl_path=p"),
    )
    rendered = repr(event)
    assert "secret chat line" not in rendered
    assert "redacted" in rendered
    assert str(event) == rendered


class _BoomSink:
    """A renderer sink that fails on USER events."""

    def __init__(self) -> None:
        self.seen: list[SimulationEvent] = []

    def emit(self, event: SimulationEvent) -> None:
        self.seen.append(event)
        if event.display is not None and event.display.kind is EventKind.USER:
            raise RuntimeError("renderer exploded")


def test_non_fatal_sink_swallows_renderer_failures_and_reports_once() -> None:
    calls: list[str] = []
    inner = _BoomSink()
    guard = NonFatalSink(inner, on_error=lambda error: calls.append(type(error).__name__))
    factory = EventFactory("r", "c")
    guard.emit(factory.build(EventKind.START, EventSource.ENGINE, text="start"))
    guard.emit(factory.build(EventKind.USER, EventSource.PERSONA, text="hi"))
    guard.emit(factory.build(EventKind.USER, EventSource.PERSONA, text="again"))
    guard.emit(factory.build(EventKind.DONE, EventSource.ENGINE, text="done"))
    assert guard.error_count == 2
    assert calls == ["RuntimeError"]  # noticed once, no recursion
    assert len(inner.seen) == 4  # the renderer still received every event
