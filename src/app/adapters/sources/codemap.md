# src/app/adapters/sources/

## Responsibility

This directory converts provider traces, recorded fixtures, and feedback annotations into the
provider-neutral evidence contracts used by the domain layer.

## Design

- `langsmith.py` validates LangSmith response shapes, translates provider failures, and maps
  selected run trees or bounded cohorts into `TraceEvidence`.
- `fixture_source.py` and `braintrust.py` provide deterministic recorded sources for offline use.
- `langsmith_feedback.py` imports fixture-backed annotations for failure review.

## Flow

Source records are flattened into `SourceRun` values, normalized, filtered by `TraceQuery`, and
returned as one trace or a cohort that stops at the requested limit.

## Integration

The CLI and evidence import services consume these sources. The adapters depend on evidence
schemas, mapping rules, source queries, and the telemetry attribute allowlist.
