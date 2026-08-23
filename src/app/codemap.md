# src/app/

## Responsibility
Core application bootstrap package containing configuration management, database session factories, and the FastAPI application factory.

## Key Files
- `main.py`: `create_app()` factory configuring routers, middleware, and lifespans.
- `config.py`: `Settings` class parsing environment variables via Pydantic.
- `database.py`: Async SQLAlchemy engine and session factory provider.

## Integration
- **Consumed by:** Uvicorn server, CLI runners, test suites.
- **Depends on:** `app.api`, `app.domain`, `app.telemetry`.
