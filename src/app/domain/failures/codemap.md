# src/app/domain/failures/

## Responsibility
Defines the failure taxonomy and categorizes simulation anomalies (e.g. policy violations, tool errors, retrieval misses).

## Key Files
- `taxonomy.py`: Canonical failure categories and classifications.
- `classifier.py`: Analyzes simulation event streams to determine failure root causes.

## Integration
- **Consumed by:** `app.domain.evidence`, `app.domain.regression`.
