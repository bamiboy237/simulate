# src/app/domain/simulation/

## Responsibility
Provides isolated execution sandboxes, transaction-scoped database fixtures, and simulated tool side-effects.

## Key Files
- `postgres.py`: `PostgresSupportSandbox` managing temporary PostgreSQL tables (`pg_temp`) and rollback transactions.
- `provisioner.py`: Factory creating disposable sandbox targets for each run.
- `scenarios.py`: Reference simulation scenarios and seed states.

## Integration
- **Consumed by:** `app.domain.execution`, `app.domain.user_simulator`.
- **Depends on:** `app.domain.support`, SQLAlchemy.
