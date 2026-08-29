# src/app/domain/execution/

## Responsibility
Starts background case runs and suite comparisons, tracks their results, and streams run events.

## Key Files
- `service.py`: `ExecutionService` creates and tracks background tasks. `ExecutionHandle.task`
  supplies completion state, including the `task.done()` condition that ends event streams.
- `errors.py`: Safe lookup errors for unknown execution IDs.

## Integration
- **Consumed by:** `app.api.runs_router`.
- **Depends on:** `app.domain.simulation`, `app.domain.suite`, and `app.domain.regression`.
