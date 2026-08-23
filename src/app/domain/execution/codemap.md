# src/app/domain/execution/

## Responsibility
Orchestrates end-to-end replay, simulation execution, and state verification.

## Key Files
- `service.py`: `ExecutionService` coordinating sandbox creation, state seeding, agent execution, and evidence collection.
- `replay.py`: Deterministic replay engine executing recorded bundles against sandbox targets.

## Integration
- **Consumed by:** `app.api.runs`, CLI runners.
- **Depends on:** `app.domain.simulation`, `app.domain.agent`, `app.domain.bundle`.
