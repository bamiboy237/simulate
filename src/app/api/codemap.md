# `src/app/api/`

## Responsibility

This directory is the HTTP delivery layer. FastAPI routes validate transport inputs,
resolve application services, delegate business work to the domain layer, and return
typed responses. `app.main.create_app()` mounts every router in this directory for web
clients, operators, and investigation sandbox bridges.

## File Map

| File | Routes and role | Domain boundary |
|---|---|---|
| `health_router.py` | `GET /healthz` and `GET /readyz`; liveness is process-only, while readiness runs `SELECT 1` with a two-second timeout. | `app.db.get_session` |
| `support_router.py` | `GET /support/orders/{order_id}`, `POST /support/tickets`, and `POST /support/refunds`. | `SupportService` over `SqlAlchemySupportRepository` |
| `workflow_router.py` | Starts, inspects, confirms, or rejects resumable support workflows under `/workflows`. | `WorkflowService` with support dependencies, telemetry, and a request-scoped PostgreSQL `AsyncPostgresSaver` |
| `agent_router.py` | `POST /agent/turns`; builds one configured support agent and optionally enables fused keyword/vector policy retrieval. | `PydanticAISupportAgent`, `SqlAlchemySupportRepository`, retrieval adapters, and `TraceRecorder` |
| `cases_router.py` | Saves, lists, and reads exact immutable case versions under `/cases`. | `RegressionCaseService` |
| `suites_router.py` | Saves, lists, and reads exact immutable suite versions under `/suites`. | `SuiteService` |
| `runs_router.py` | Starts case runs and suite comparisons, polls both through one shared status handler, and streams run events. | `ExecutionService`, `RegressionCaseService`, and `SuiteService` |
| `failures_router.py` | Lists, reads, and reviews failure-group proposals under `/failure-groups`. | `FailureReviewService` |
| `investigations_router.py` | Public lifecycle, chat, detail, listing, and SSE routes under `/api/v1/investigations`; authenticated sandbox bridge routes under `/internal`. | `InvestigationService` and runner event schemas |
| `dependencies.py` | FastAPI composition root for case, suite, failure, execution, and investigation services. | SQLAlchemy repositories, settings, sandbox provisioner, and model configuration |

## Design Patterns

- **Thin Controller:** Routes translate HTTP values and delegate authorization, lifecycle
  rules, immutable versioning, confirmation gates, and execution policy to domain services.
- **Dependency Injection:** FastAPI `Depends` constructs request-scoped repository-backed
  services. `ExecutionService` is cached on `app.state` because its in-memory execution
  registry must survive across polling requests.
- **Repository:** Services receive SQLAlchemy repositories instead of issuing persistence
  queries from route handlers.
- **Background Execution Handle:** Run and comparison start routes return an execution UUID.
  One handler serves both status routes, and `/runs/{execution_id}/events` exposes the
  allowlisted event stream as SSE.
- **Cursor-Based Streaming:** Investigation events use persisted sequence numbers.
  `Last-Event-ID` resumes public SSE, and the bridge inbox uses a message ID cursor.
- **State Machine:** `InvestigationService` enforces valid pending, provisioning, running,
  and terminal transitions.

## Data and Control Flow

### Standard service-backed request

1. `app.main.create_app()` mounts the router and supplies request logging and the common
   exception handler.
2. FastAPI validates the request with route-local or domain Pydantic schemas.
3. A dependency resolves settings and an `AsyncSession`, then builds the repository and
   domain service.
4. The route calls the service and returns its typed result. Domain errors cross the shared
   application exception boundary.

### Case run and suite comparison

1. `POST /runs` resolves an exact case through `RegressionCaseService.get_case()`, then calls
   `ExecutionService.start_run()`.
2. `POST /comparisons` resolves an exact suite and every referenced case version, then calls
   `ExecutionService.start_comparison()` with the declared candidate change.
3. `ExecutionService` owns the background task, provisioner, result/error handle, and event
   queue. Poll and SSE requests reuse the app-scoped service.

### Investigation creation and operator stream

1. `POST /api/v1/investigations` passes `InvestigationCreateRequest` to
   `InvestigationService.start()`.
2. The service rejects reserved environment or secret overrides, resolves the task brief,
   validates Modal callback and outbound-domain policy, hashes a new bridge token, persists
   the pending record, and launches a `FakeRunner` or `ModalRunner` sandbox.
3. `GET /api/v1/investigations/{id}/events` replays events after `Last-Event-ID`, emits
   keepalives while polling, flushes trailing events, and closes at a terminal state or client
   disconnect.
4. `POST /api/v1/investigations/{id}/messages` stores prompt, steer, or follow-up input;
   terminal investigations reject new messages.

### Authenticated sandbox bridge

1. The bridge sends its bearer token to `GET /internal/inbox` or `POST /internal/events`.
2. `InvestigationService.verify_bridge_token()` hashes the token and resolves its investigation.
3. Inbox polling reads user messages after the cursor. Event ingestion inserts
   `(investigation_id, seq)` pairs idempotently and updates heartbeat data.
4. The first event batch advances pending/provisioning records to running. A summary is
   persisted, but completion requires an investigation-finished event; fatal errors fail the
   run. Terminal transitions terminate the tracked sandbox handle.

### Resumable support workflow

1. `get_workflow_service()` converts the asyncpg URL to the psycopg URL required by LangGraph
   and opens a request-scoped `AsyncPostgresSaver`.
2. `POST /workflows` starts the workflow; `GET /workflows/{id}` reloads its checkpoint.
3. Confirm or reject routes pass the actor and request binding to `WorkflowService`, which
   owns resume and authorization rules.

## Integration Points

- **Mounted by:** `src/app/main.py`.
- **Consumers:** Web UI, API clients, terminal investigation operators, sandbox bridge
  processes, and health probes.
- **Persistence:** `app.db` sessions and domain repositories; LangGraph checkpoints use
  PostgreSQL directly.
- **Execution:** `app.domain.execution`, `app.domain.simulation`, and `app.domain.runner`.
- **Agent and retrieval:** `app.adapters.pydantic_ai_agent`, `app.domain.retrieval`, and
  `app.telemetry`.
