"""This module records spans for the reference agent.

The agent and its tools emit spans only through ``TraceRecorder``.
If the application disables tracing, the recorder has no OpenTelemetry tracer.
Each recorder method then returns without telemetry work.
"""

from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager

from opentelemetry import trace
from opentelemetry.trace import INVALID_TRACE_ID, Span, SpanContext, Tracer

from app.telemetry.allowlist import sanitize_attributes

Scalar = bool | int | float | str


class TraceSpan:
    """This class writes attributes after sanitization for one span.

    Every span stores the sanitized attributes, the error reason code, and
    its name in the instance, so a listener can inspect the span after it
    closes. The OpenTelemetry wrapper mirrors the same values to the exported
    span; the null span only stores them.
    """

    def __init__(
        self,
        name: str,
        forbidden_substrings: tuple[str, ...] = (),
    ) -> None:
        self._name = name
        self._forbidden = forbidden_substrings
        self._attributes: dict[str, Scalar] = {}
        self._error_code: str | None = None

    @property
    def name(self) -> str:
        """This property returns the sanitized span name."""
        return self._name

    @property
    def attributes(self) -> Mapping[str, Scalar]:
        """This property returns the sanitized attributes recorded so far."""
        return dict(self._attributes)

    @property
    def error_code(self) -> str | None:
        """This property returns the stable reason code, if the span failed."""
        return self._error_code

    def set_attribute(self, name: str, value: object) -> dict[str, Scalar]:
        """This method records one allowlisted attribute and drops every other attribute.

        The method returns the sanitized values that were accepted.
        """
        sanitized = sanitize_attributes({name: value}, self._forbidden)
        self._attributes.update(sanitized)
        return sanitized

    def set_error(self, reason_code: str) -> None:
        """This method marks the span as failed with a stable, safe reason code."""
        self._error_code = reason_code

    def span_context(self) -> SpanContext | None:
        """This method returns the underlying span context.

        If the application disables tracing, this method returns ``None``.
        """

class NullSpan(TraceSpan):
    """This class represents a span that performs no telemetry work.

    The span still stores sanitized attributes and the error reason code so
    that simulation runs can build their normalized event transcript even
    when OpenTelemetry is disabled.
    """


class _OtelSpanWrapper(TraceSpan):
    def __init__(
        self,
        span: Span,
        name: str,
        forbidden_substrings: tuple[str, ...],
    ) -> None:
        super().__init__(name, forbidden_substrings)
        self._span = span

    def set_attribute(self, name: str, value: object) -> dict[str, Scalar]:
        sanitized = super().set_attribute(name, value)
        for key, clean_value in sanitized.items():
            self._span.set_attribute(key, clean_value)
            # LangSmith indexes only attributes with its own prefixes. Mirror
            # the allowlisted value so imported runs keep the same evidence.
            self._span.set_attribute(f"langsmith.metadata.{key}", clean_value)
        return sanitized

    def set_error(self, reason_code: str) -> None:
        super().set_error(reason_code)
        from opentelemetry.trace import Status, StatusCode

        self._span.set_status(Status(StatusCode.ERROR, reason_code))

    def span_context(self) -> SpanContext | None:
        return self._span.get_span_context()

SpanListener = Callable[[TraceSpan, bool], None]


class TraceRecorder:
    """This class records sanitized spans.

    If the application disables tracing, this class skips telemetry work.
    A caller may pass ``span_listener`` to observe every span start and end;
    the listener receives the span and a boolean that is ``True`` when the
    span has ended. The listener receives the same sanitized span in both
    calls, so ended spans expose their final attributes and error code.
    """

    def __init__(
        self,
        tracer: Tracer | None,
        forbidden_substrings: tuple[str, ...] = (),
        span_listener: SpanListener | None = None,
    ) -> None:
        self._tracer = tracer
        self._forbidden = tuple(forbidden_substrings)
        self._listener = span_listener

    @property
    def enabled(self) -> bool:
        return self._tracer is not None

    @contextmanager
    def span(
        self,
        name: str,
        attributes: Mapping[str, object] | None = None,
    ) -> Iterator[TraceSpan]:
        """This method starts a sanitized child span for the current context."""
        if self._tracer is None:
            span: TraceSpan = NullSpan(name, self._forbidden)
            self._apply_initial_attributes(span, attributes)
            self._notify(span, ended=False)
            try:
                yield span
            finally:
                self._notify(span, ended=True)
            return
        # record_exception=False: exceptions that escape a span would otherwise
        # be exported as exception events with full stack traces, bypassing the
        # allowlist. Status is set explicitly with stable reason codes instead.
        with self._tracer.start_as_current_span(
            name,
            record_exception=False,
            set_status_on_exception=False,
        ) as otel_span:
            span = _OtelSpanWrapper(otel_span, name, self._forbidden)
            self._apply_initial_attributes(span, attributes)
            self._notify(span, ended=False)
            try:
                yield span
            finally:
                self._notify(span, ended=True)

    def _apply_initial_attributes(
        self,
        span: TraceSpan,
        attributes: Mapping[str, object] | None,
    ) -> None:
        if attributes:
            for key, value in sanitize_attributes(attributes, self._forbidden).items():
                span.set_attribute(key, value)

    def _notify(self, span: TraceSpan, *, ended: bool) -> None:
        if self._listener is not None:
            self._listener(span, ended)

    def current_trace_id(self) -> str | None:
        """This method returns the hexadecimal trace ID of the current span.

        If no current span exists, this method returns ``None``.
        """
        if self._tracer is None:
            return None
        span_context = trace.get_current_span().get_span_context()
        if span_context.trace_id in (0, INVALID_TRACE_ID):
            return None
        return f"{span_context.trace_id:032x}"
