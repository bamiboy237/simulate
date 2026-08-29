# src/app/telemetry/

## Responsibility

This directory records agent spans and filters trace attributes before export.

## Key Files

- `allowlist.py`: Trace attribute allowlist, scalar checks, secret filtering, and value limits.
- `config.py`: Optional console and LangSmith processors, provider caching, and tracer creation.
- `recorder.py`: Context-managed OpenTelemetry or null spans with sanitized attributes and safe
  error codes. Context managers own span completion.

## Integration

- **Consumed by:** Agent, workflow, retrieval, and simulation services.
- **Depends on:** OpenTelemetry SDK and `app.config`.
