# alembic/

## Responsibility
Database schema migration management using Alembic and SQLAlchemy.

## Key Files
- `env.py`: Migration environment runner connecting to `DATABASE_URL`.
- `versions/`: Versioned Python migration scripts altering database schemas.

## Integration
- **Executed by:** `uv run alembic upgrade head`.
- **Depends on:** `app.domain.support.models`, `app.config`.
