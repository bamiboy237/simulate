# src/app/api/

## Responsibility
HTTP delivery layer providing thin FastAPI route handlers, request validation, and dependency injection wrappers.

## Design Patterns
- **Thin Controller Pattern:** Routes validate HTTP inputs, call domain services, and return typed Pydantic responses.
- **Dependency Injection:** `dependencies.py` resolves domain services, database sessions, and execution services.

## Routes
- `cases_router.py`: Endpoints for managing and creating evaluation cases (`/cases`).
- `runs_router.py`: Endpoints for triggering simulation runs and polling results (`/runs`).
- `investigations_router.py`: Endpoints for creating investigations, chat inbox, and status polling (`/investigations`), an SSE event stream (`GET /investigations/{id}/events`) that replays persisted events after the client's `Last-Event-ID` then tails new ones, and bearer-authenticated bridge endpoints for event batch ingestion (`POST /internal/events`) and long-poll inbox reads (`GET /internal/inbox`).
- `agent_router.py`: Endpoints for agent execution and evaluations.
- `failures_router.py`: Endpoints for failure categorization and taxonomy.
- `suites_router.py`: Endpoints for test suites.
- `support_router.py`: Endpoints for customer support resources.
- `workflow_router.py`: Endpoints for workflow graphs.
- `health_router.py`: Health check probe endpoint.

## Integration
- **Consumed by:** Web UI, external API clients, sandbox bridge processes.
- **Depends on:** `app.domain.execution`, `app.domain.bundle`, `app.domain.evidence`, `app.domain.investigation`.
