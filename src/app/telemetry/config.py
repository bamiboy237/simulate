"""This module configures trace providers for local inspection and LangSmith export.

The application enables export only when settings request it.
If settings lack required values, this module disables the related exporter.
The application works without tracing.
"""

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import SpanProcessor, TracerProvider
from opentelemetry.sdk.trace.export import (
    BatchSpanProcessor,
    ConsoleSpanExporter,
    SimpleSpanProcessor,
)

if TYPE_CHECKING:
    from app.config import Settings

SERVICE_NAME = "simulate"

_LANGSMITH_OTLP_ENDPOINT = "https://api.smith.langchain.com/otel/v1/traces"


@dataclass(frozen=True)
class LangSmithExportConfig:
    """This class stores OpenTelemetry export settings.

    The application resolves these settings for LangSmith.
    """

    endpoint: str
    headers: dict[str, str] = field(repr=False)
    project: str

    def otlp_exporter(self) -> OTLPSpanExporter:
        """This method builds the OTLP exporter from this configuration."""
        return OTLPSpanExporter(endpoint=self.endpoint, headers=self.headers, timeout=10)


def langsmith_export_config(settings: "Settings") -> LangSmithExportConfig | None:
    """This function returns LangSmith export settings.

    If the settings lack any required value, this function returns ``None``.
    """
    if not settings.langsmith_tracing:
        return None
    api_key = settings.langsmith_api_key
    if api_key is None or not api_key.get_secret_value():
        return None
    return LangSmithExportConfig(
        endpoint=settings.langsmith_otlp_endpoint or _LANGSMITH_OTLP_ENDPOINT,
        headers={
            "x-api-key": api_key.get_secret_value(),
            "Langsmith-Project": settings.langsmith_project,
        },
        project=settings.langsmith_project,
    )


def _span_processors(settings: "Settings") -> list[SpanProcessor]:
    """This function returns enabled span processors."""
    processors: list[SpanProcessor] = []
    if settings.otel_tracing_enabled:
        # Simple processor prints each finished span immediately, which is
        # what local trace inspection needs.
        processors.append(SimpleSpanProcessor(ConsoleSpanExporter()))
    langsmith = langsmith_export_config(settings)
    if langsmith is not None:
        processors.append(BatchSpanProcessor(langsmith.otlp_exporter()))
    return processors


def build_trace_provider(settings: "Settings") -> TracerProvider | None:
    """This function builds a configured provider.

    If the configuration enables no span processor, this function returns ``None``.
    """
    processors = _span_processors(settings)
    if not processors:
        return None
    resource = Resource.create(
        {
            "service.name": SERVICE_NAME,
            "service.environment": settings.environment,
            "agent.workflow.version": "2.0.0",
            "agent.model.provider": settings.model_provider or "unconfigured",
            "agent.model.name": settings.model_name or "unconfigured",
            "langsmith.project": settings.langsmith_project,
        }
    )
    provider = TracerProvider(resource=resource)
    for processor in processors:
        provider.add_span_processor(processor)
    return provider


_cache: dict[str, TracerProvider] = {}


def _fingerprint(settings: "Settings") -> str:
    langsmith = langsmith_export_config(settings)
    return "|".join(
        (
            str(settings.otel_tracing_enabled),
            str(langsmith.endpoint if langsmith else ""),
            str(langsmith.project if langsmith else ""),
            str(bool(langsmith)),
            settings.environment,
            settings.model_provider or "",
            settings.model_name or "",
        )
    )


def build_tracer(settings: "Settings") -> trace.Tracer | None:
    """This function returns the application tracer.

    If the application disables tracing, this function returns ``None``.
    This function caches one provider for each configuration.
    Repeated calls do not create duplicate exporters.
    This function does not install the provider as the global provider.
    The application creates spans only through the returned tracer.
    """
    fingerprint = _fingerprint(settings)
    provider = _cache.get(fingerprint)
    if provider is None:
        provider = build_trace_provider(settings)
        if provider is None:
            return None
        _cache[fingerprint] = provider
    return provider.get_tracer(SERVICE_NAME, "2.0.0")


def reset_trace_cache() -> None:
    """This function removes cached providers so tests isolate configurations."""
    for provider in _cache.values():
        provider.shutdown()
    _cache.clear()
