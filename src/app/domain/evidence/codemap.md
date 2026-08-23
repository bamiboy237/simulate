# src/app/domain/evidence/

## Responsibility
Generates, stores, and validates written failure evidence summaries and automated evaluation assertions.

## Key Files
- `service.py`: Creates structured evidence summaries from simulation runs.
- `models.py`: Evidence entities and storage schemas.
- `repository.py`: Database persistence for evaluation evidence.

## Integration
- **Consumed by:** `app.api.evidence`, `app.domain.execution`.
