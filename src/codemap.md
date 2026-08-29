# src/

## Responsibility
Root source directory for Simulate's API, CLI, adapters, telemetry, persistence wiring,
and domain services.

## Sub-Packages
- [`app/`](app/codemap.md): Application runtime, delivery layers, integrations, and domain logic.

## Boundaries

- `app/api/` and `app/cli/` translate external input into domain calls.
- `app/domain/` owns business rules, state transitions, evaluation, and isolation policies.
- `app/adapters/` and `app/telemetry/` connect external systems without moving their rules into
  the domain.
- [`alembic/`](../alembic/codemap.md) versions the PostgreSQL schema used by repositories.
