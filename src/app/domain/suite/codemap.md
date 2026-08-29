# src/app/domain/suite/

## Responsibility
Stores immutable case suites and runs baseline and candidate comparisons across their members.

## Key Files
- `service.py`: Validates exact case versions and stores versioned suite membership.
- `runner.py`: Validates suite membership, experiment shape, and the runtime baseline before any
  case runs. Both public comparison entry points use `_validate_model_baseline()`.
- `schemas.py`: Suite membership, result totals, and verdict contracts.

## Integration
- **Consumed by:** `app.api.runs_router`, execution services, and CLI model-lab commands.
