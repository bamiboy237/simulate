# src/app/domain/investigation/

## Responsibility
Investigation Domain & Control Plane Service. Manages the lifecycle state machine, task brief assembly, cloud runner provisioning, token authentication, event collection, chat message inbox, findings persistence, stale run detection, and automated sandbox termination for agent investigations.

## Design Patterns
- **Finite State Machine (FSM):** `InvestigationService` enforces strict status transitions (`pending` -> `provisioning` -> `running` -> `completed` / `failed` / `cancelled`) with immutable terminal states preventing post-completion modifications.
- **Dynamic Runner Resolution & Strategy Pattern:** `InvestigationService._resolve_runner()` dynamically initializes `ModalRunner` when `MODAL_ENABLED=true` or falls back to `FakeRunner` for offline environments, allowing dependency injection for tests.
- **Resource Lifecycle Management / Finalizer Pattern:** On transitioning to terminal states (`completed`, `failed`, `cancelled`), `InvestigationService` automatically terminates active sandbox compute handles via `CloudRunner.terminate(handle)`.
- **Repository Pattern:** `InvestigationRepository` protocol and `SqlAlchemyInvestigationRepository` encapsulate all database queries, leveraging PostgreSQL `on_conflict_do_nothing` for idempotent event recording.
- **Domain Service Pattern:** `InvestigationService` coordinates business logic across state verification, task brief rendering, bearer token SHA-256 hashing, sandbox provisioning, event ingestion, and background sweeping.
- **Token Guard / Authentication:** SHA-256 hashed bridge tokens authenticate sandbox incoming event batches and inbox polling without exposing raw tokens in database tables.

## Key Files
- `service.py`: `InvestigationService` managing FSM transitions, runner resolution, sandbox provisioning, event processing, chat, stale sweeping, and sandbox termination.
- `brief.py`: `render_task_brief()` assembling structured markdown task briefs (incident overview, environment slice, failure summary, rules, evaluator expectations, and mandatory `summary.submit` final action protocol).
- `repository.py`: `InvestigationRepository` protocol and `SqlAlchemyInvestigationRepository` persistence operations.
- `models.py`: SQLAlchemy ORM entities (`InvestigationRecord`, `InvestigationEventRecord`, `InvestigationMessageRecord`, `InvestigationSummaryRecord`).
- `schemas.py`: Pydantic validation models for request/response payloads and status enums (`InvestigationStatus`, `InvestigationCreateRequest`, `InvestigationDetailResponse`, `InvestigationSummaryResponse`).
- `errors.py`: Domain exception hierarchy (`InvestigationNotFoundError`, `InvalidStateTransitionError`, `TerminalStateImmutableError`, `InvalidBridgeTokenError`).

## Data & Control Flow
1. **Investigation Launch:**
   - `InvestigationService.start()` generates a cryptographically secure URL-safe bridge token, computes its SHA-256 hash, and inserts a new `InvestigationRecord` in `pending` status.
   - Compiles structured task brief via `render_task_brief()` (if not provided).
   - Resolves runner (`ModalRunner` or `FakeRunner`) and provisions container sandbox via `runner.create_sandbox(SandboxSpec(...))`, storing the handle in `_handles`.
   - On sandbox creation failure, immediately marks the investigation as `failed`.
2. **State Transitions & Sandbox Reclamation:**
   - As sandbox provisioning proceeds, `transition_status()` moves state from `pending` -> `provisioning` -> `running`, stamping `started_at`.
   - When entering terminal states (`completed`, `failed`, `cancelled`), `transition_status()` stamps `finished_at` and terminates the allocated sandbox handle via `runner.terminate(handle)`.
3. **Event Ingestion:**
   - Sandbox bridge posts event batches via `record_events(token, events)`. The service verifies token authenticity and executes idempotent bulk inserts.
   - Automatically promotes `pending`/`provisioning` to `running` on first event receipt.
   - Updates heartbeat timestamps on `EventType.heartbeat`.
   - Transitions state to `completed` on `EventType.summary_submitted` (saving `InvestigationSummaryRecord`) or `failed` on `EventType.error`.
4. **Interactive Steering:**
   - Users submit messages via `record_user_message()` (`prompt` or `steer` mode).
   - The sandbox bridge retrieves unread messages via `poll_inbox(token, cursor=...)`.
5. **Staleness Sweep:**
   - Periodic background sweeper calls `sweep_stale(silence_seconds=90)` to transition silent running investigations to `failed`.

## Integration
- **Consumed by:** `app.api.investigations_router` (REST API routes for UI / external clients), `app.cli.investigate` (`lab investigate` subcommands), `scripts.investigate_smoke` (live E2E verification), background worker tasks / sweeper crons.
- **Depends on:** `app.domain.investigation.models`, `app.domain.investigation.repository`, `app.domain.investigation.brief`, `app.domain.runner` (`CloudRunner`, `ModalRunner`, `SandboxSpec`, `SandboxHandle`, `EventType`), `tests.fakes.runners` (`FakeRunner`), `app.config`, `app.db.Base`, SQLAlchemy `AsyncSession`.
