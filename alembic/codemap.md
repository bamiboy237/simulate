# alembic/

## Responsibility
PostgreSQL schema versioning with Alembic and asynchronous SQLAlchemy.

## Key Files
- `env.py`: Loads the migration database URL, registers support, evidence, failure, and retrieval
  model metadata, and runs migrations online or offline.
- [`versions/`](versions/codemap.md): Linear revision history from the empty baseline through the
  investigation schema.

## Integration
- **Executed by:** `uv run alembic upgrade head`.
- **Depends on:** `app.config`, `app.domain.support.models`, and the imported metadata modules.
- **Changes:** Revisions create support, evidence, regression, suite, failure-review, retrieval,
  and investigation tables. See the detailed revision map for exact ownership.
