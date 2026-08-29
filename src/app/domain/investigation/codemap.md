# src/app/domain/investigation/

## Responsibility

Implements the control-plane domain for one agent investigation. It validates launch inputs,
renders the task brief, persists lifecycle state and ordered events, authenticates the sandbox
bridge, stores chat and the final summary, exposes replay data, detects stale runs, and asks the
selected runner to create or terminate compute.

## Design

- **Domain service / finite-state machine:** `InvestigationService` allows
  `pending -> provisioning|failed|cancelled`, `provisioning -> running|failed|cancelled`, and
  `running -> completed|failed|cancelled`. Terminal records are immutable.
- **Repository pattern:** `InvestigationRepository` defines async persistence.
  `SqlAlchemyInvestigationRepository` uses PostgreSQL `ON CONFLICT DO NOTHING` for idempotent
  `(investigation_id, seq)` event insertion.
- **Runner strategy:** the service accepts an injected `CloudRunner`; otherwise it resolves
  `ModalRunner` when enabled and `FakeRunner` offline.
- **Credential guard:** each run gets a random bridge token. Only its SHA-256 digest is stored.
- **Cloud launch gate:** Modal requires a public HTTP(S) control-plane URL and a bare-domain
  allowlist containing the control-plane and model-provider hosts.
- **Persistence-first stream:** the public Server-Sent Events route replays stored sequence rows;
  there is no separate in-memory event bus.
- **Canonical status contract:** `InvestigationStatus` exposes uppercase enum members, and
  `TERMINAL_STATUSES` is shared by the service, API stream, and CLI stream.
- **Resource finalizer:** terminal transitions terminate a sandbox only when its handle exists in
  the current service instance's `_handles` map.

## Key Files

- `service.py`: launch validation, state transitions, event interpretation, token checks, chat,
  stale sweep, and handle termination.
- `repository.py`: persistence protocol and SQLAlchemy implementation.
- `models.py`: ORM records for investigations, events, messages, and one summary.
- `schemas.py`: request, response, event, chat, summary, and lifecycle contracts.
- `brief.py`: Markdown brief with mandatory final `summary.submit`.
- `errors.py`: typed 401, 404, and 409 domain errors.

## Data and Control Flow

### Launch and Provisioning

1. The API or CLI builds `InvestigationCreateRequest` and calls `start()` manually.
2. The service rejects reserved keys, resolves Modal network policy and model credentials, resolves
   the outer timeout, renders a brief when absent, and validates the complete `SandboxSpec`.
3. It creates a `pending` database row, then launches the runner in a worker thread. A successful
   handle enters `_handles`; a launch exception changes the row to `failed`.
4. The first accepted bridge batch advances `pending` through `provisioning` to `running`, or
   advances `provisioning` to `running`, and stamps `started_at`.

### Events and Completion

1. The sandbox posts `list[RunnerEvent]` directly to `/internal/events` with its bridge token.
2. The service authenticates the token, then stores new sequence numbers and ignores duplicates.
3. Heartbeats update `last_heartbeat_at`. `summary_submitted` creates the one-to-one summary but
   does not finish the run by itself.
4. `investigation_finished` completes only when a summary already exists or is in the same batch.
   An end without a summary fails the run. An `error` event also fails it.
5. A completed row accepts only trailing `investigation_finished` and `error` events. Other
   terminal writes are ignored.
6. A terminal transition stamps `finished_at` and terminates the known in-memory handle.

### Chat, Replay, and Stale Runs

1. The public message endpoint or CLI stores user messages for non-terminal investigations.
2. The bridge polls `/internal/inbox?cursor=<message-id>` and receives later messages in ID order.
3. The SSE route and CLI read events after a sequence cursor. SSE emits keepalives, replays missed
   rows, flushes trailing rows after terminal state, and closes.
4. FastAPI startup runs `sweep_stale()`, which fails `running` rows older than the heartbeat or
   start-time cutoff.

## Persistence Model and Migration

`alembic/versions/d8f4e2a1b9c7_add_investigation_tables.py` creates:

- `investigations`: lifecycle, references, token hash, error, heartbeat, and timestamps.
- `investigation_events`: rows unique by investigation and sequence, with an ordered index.
- `investigation_messages`: chat rows indexed by investigation and message ID.
- `investigation_summaries`: one summary per investigation through a unique foreign key.

All child tables use `ON DELETE CASCADE`. Downgrade removes children before the parent. The
executable revision points to `9f7c2a1d5e4b` through `down_revision`.

## Integration

- **HTTP:** `app.api.investigations_router` exposes public and bridge routes.
- **Injection:** `app.api.dependencies.get_investigation_service()`.
- **CLI:** `app.cli.investigate`.
- **Startup:** `app.main.lifespan()` runs the stale sweep.
- **Execution:** `app.domain.runner`.
- **Database:** `app.db.Base`, SQLAlchemy, and the investigation migration.
