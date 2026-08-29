# src/app/

## Responsibility
Application package containing runtime bootstrap, delivery layers, external adapters, telemetry,
database sessions, and domain logic.

## Key Files
- `main.py`: `create_app()` factory configuring routers, middleware, and lifespans.
- `config.py`: `Settings` class parsing environment variables via Pydantic.
- `db.py`: Cached async SQLAlchemy engine and session factory providers.
- `logging.py`: Structured logging and request logger configuration.
- `errors.py`: Global API exception translation.

## Sub-Packages

- [`api/`](api/codemap.md): FastAPI routes and dependency construction.
- [`cli/`](cli/codemap.md): `lab` commands and simulation interfaces.
- [`adapters/`](adapters/codemap.md): Model and trace-source integrations.
- [`telemetry/`](telemetry/codemap.md): Trace recording and attribute sanitization.
- [`domain/`](domain/codemap.md): Business models, services, policies, runners, and sandboxes.

## Integration
- **Consumed by:** Uvicorn server, CLI runners, test suites.
- **Depends on:** `app.api`, `app.domain`, `app.telemetry`.
