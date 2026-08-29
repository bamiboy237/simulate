"""Generic event stream for the user simulator.

One occurrence is a ``SimulationEvent`` that carries two views:

- a ``DisplayEvent`` (ephemeral, in-memory only) with human-readable text.
  It is deliberately blocked from serialization: chat and tool text never
  leave memory through this object.
- a ``PersistentEvent`` with only allowlisted scalar fields that the
  append-only JSONL sink may write.

This module is generic: it contains no support/reference logic and no CLI.
"""

from __future__ import annotations

import itertools
import json
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from typing import Protocol, runtime_checkable

# Fields the persistent JSONL sink may store.  Chat text ("message") is
# deliberately absent: actual conversation lives only in display memory.
PERSISTENT_FIELDS = frozenset(
    {
        "turn",
        "tool",
        "outcome",
        "reason",
        "error",
        "model_provider",
        "model_name",
        "tokens",
        "latency_ms",
        "attempts",
        "transition",
        "state",
        "workflow_id",
        "verified",
    }
)

_SCALAR_TYPES = (bool, int, float, str)


class EventKind(str, Enum):
    """The stable kind of one operational event."""

    START = "start"
    USER = "user"
    AGENT = "agent"
    MODEL = "model"
    TOOL_SELECTED = "tool_selected"
    TOOL_RESULT = "tool_result"
    APPROVAL = "approval"
    RETRY = "retry"
    STATE = "state"
    DONE = "done"
    ERROR = "error"
    CLEANUP = "cleanup"


class EventSource(str, Enum):
    """Who produced one event."""

    PERSONA = "persona"
    SUPPORT = "support"
    REFERENCE = "reference"
    ENGINE = "engine"
    CLI = "cli"


@dataclass(frozen=True)
class DisplayEvent:
    """Ephemeral display-only memory for one event.

    Serialization is deliberately blocked: this object may carry chat text
    and tool arguments, so any attempt to serialize it raises ``TypeError``.
    """

    kind: EventKind
    source: EventSource
    seq: int
    timestamp: datetime
    text: str = ""
    detail: tuple[str, ...] = ()

    def to_json(self) -> str:
        raise TypeError(
            "DisplayEvent is display-only memory and must never be serialized"
        )

    def model_dump(self) -> dict[str, object]:
        raise TypeError(
            "DisplayEvent is display-only memory and must never be serialized"
        )

    def __repr__(self) -> str:
        """Redacted repr: chat/tool text never leaks into logs or crash output."""
        return (
            f"DisplayEvent(kind={self.kind.value!r}, source={self.source.value!r}, "
            f"seq={self.seq}, timestamp={self.timestamp.isoformat()}, "
            f"text=[redacted], detail=[redacted])"
        )


@dataclass(frozen=True)
class PersistentEvent:
    """One allowlisted, serializable payload."""

    kind: EventKind
    source: EventSource
    seq: int
    run_id: str
    case_id: str
    timestamp: datetime
    fields: tuple[tuple[str, object], ...] = ()

    def to_json(self) -> str:
        """Serialize only allowlisted scalar fields."""
        payload: dict[str, object] = {
            "timestamp": self.timestamp.isoformat(),
            "event": self.kind.value,
            "source": self.source.value,
            "seq": self.seq,
            "run_id": self.run_id,
            "case_id": self.case_id,
        }
        for key, value in self.fields:
            if key not in PERSISTENT_FIELDS:
                continue
            if not isinstance(value, _SCALAR_TYPES):
                continue
            payload[key] = value
        return json.dumps(payload, sort_keys=True)


@dataclass(frozen=True)
class SimulationEvent:
    """One occurrence: a persistent payload plus an optional display view."""

    persistent: PersistentEvent
    display: DisplayEvent | None = None


class EventFactory:
    """Builds sequence-numbered events for one run."""

    def __init__(self, run_id: str, case_id: str) -> None:
        self.run_id = run_id
        self.case_id = case_id
        self._counter = itertools.count(1)

    def next_sequence(self) -> int:
        return next(self._counter)

    def build(
        self,
        kind: EventKind,
        source: EventSource,
        *,
        text: str = "",
        detail: Iterable[str] = (),
        **fields: object,
    ) -> SimulationEvent:
        seq = self.next_sequence()
        now = datetime.now(UTC)
        display = DisplayEvent(
            kind=kind,
            source=source,
            seq=seq,
            timestamp=now,
            text=text,
            detail=tuple(detail),
        )
        persistent = PersistentEvent(
            kind=kind,
            source=source,
            seq=seq,
            run_id=self.run_id,
            case_id=self.case_id,
            timestamp=now,
            fields=tuple(fields.items()),
        )
        return SimulationEvent(persistent=persistent, display=display)


@runtime_checkable
class EventSink(Protocol):
    """Receives every emitted simulation event."""

    def emit(self, event: SimulationEvent) -> None: ...


class NonFatalSink:
    """Runs one sink but never lets a renderer exception escape the run.

    A failing display/renderer sink must not change business execution or
    persistence: the exception is counted and reported once through
    ``on_error`` (guarded against recursion), then ignored.
    """

    def __init__(
        self,
        inner: EventSink,
        on_error: Callable[[Exception], None] | None = None,
    ) -> None:
        self._inner = inner
        self._on_error = on_error
        self._notifying = False
        self.error_count = 0

    def set_on_error(self, on_error: Callable[[Exception], None] | None) -> None:
        """Replace the error notice callback (used by the run emitter)."""
        self._on_error = on_error

    def emit(self, event: SimulationEvent) -> None:
        try:
            self._inner.emit(event)
        except Exception as error:  # noqa: BLE001 - renderer failures are non-fatal
            self.error_count += 1
            if self.error_count == 1 and self._on_error is not None and not self._notifying:
                self._notifying = True
                try:
                    self._on_error(error)
                except Exception:  # noqa: BLE001 - the notice must never recurse
                    pass
                finally:
                    self._notifying = False


class CompositeSink:
    """Fans one event out to every registered sink."""

    def __init__(self, sinks: Iterable[EventSink] = ()) -> None:
        self._sinks: list[EventSink] = list(sinks)

    def add(self, sink: EventSink) -> None:
        self._sinks.append(sink)

    def emit(self, event: SimulationEvent) -> None:
        for sink in self._sinks:
            sink.emit(event)

    def __len__(self) -> int:
        return len(self._sinks)


class EventEmitter:
    """Sequence + factory + fan-out for one run."""

    def __init__(
        self,
        run_id: str,
        case_id: str,
        sinks: Iterable[EventSink] = (),
    ) -> None:
        self.run_id = run_id
        self.case_id = case_id
        self._factory = EventFactory(run_id, case_id)
        self._sink = CompositeSink(sinks)

    @property
    def sink(self) -> CompositeSink:
        return self._sink

    def add(self, sink: EventSink) -> None:
        self._sink.add(sink)

    def emit(
        self,
        kind: EventKind,
        source: EventSource,
        *,
        text: str = "",
        detail: Iterable[str] = (),
        **fields: object,
    ) -> SimulationEvent:
        event = self._factory.build(kind, source, text=text, detail=detail, **fields)
        self._sink.emit(event)
        return event


class JsonlPersistentSink:
    """Append-only JSONL sink that writes only persistent fields.

    The log file is created lazily on the first emitted event, so callers
    can validate settings before any artifact exists.
    """

    def __init__(
        self,
        run_id: str,
        case_id: str,
        root: Path = Path("artifacts/user-simulator"),
    ) -> None:
        self.run_id = run_id
        self.case_id = case_id
        self._root = root
        self.path = root / f"{run_id}.jsonl"

    def emit(self, event: SimulationEvent) -> None:
        self._root.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as stream:
            stream.write(event.persistent.to_json() + "\n")
            stream.flush()


class DisplayMemory:
    """In-memory display-only buffer; chat and tool text never leaves memory."""

    def __init__(self) -> None:
        self._events: list[DisplayEvent] = []

    def emit(self, event: SimulationEvent) -> None:
        if event.display is not None:
            self._events.append(event.display)

    @property
    def events(self) -> tuple[DisplayEvent, ...]:
        return tuple(self._events)

    @property
    def chat(self) -> tuple[DisplayEvent, ...]:
        """Return only the actual USER/AGENT conversation lines."""
        return tuple(
            event
            for event in self._events
            if event.kind in {EventKind.USER, EventKind.AGENT}
        )

    def __len__(self) -> int:
        return len(self._events)
