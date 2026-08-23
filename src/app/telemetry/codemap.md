# src/app/telemetry/

## Responsibility
Observability and tracing subsystem instrumenting agent execution spans and enforcing data privacy.

## Key Files
- `tracing.py`: OpenTelemetry tracer setup and span lifecycle hooks.
- `processors.py`: Span processors scrubbing secrets and filtering sensitive keys before export.

## Integration
- **Consumed by:** FastAPI middleware, domain services, execution coordinator.
- **Depends on:** OpenTelemetry SDK, `app.config`.
