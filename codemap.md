# Repository Atlas: Simulate

## Project Responsibility
Simulate is an agent evaluation and simulation platform for autonomous AI support workflows. It provides deterministic, reproducible sandbox environments, synthetic user simulation, privacy-safe bundle compilation, and cloud runner orchestration. The Phase 8 Modal investigation MVP is complete and live-verified (Prime Agent RPC, World Gateway, SSE event streaming); the full Phase 8 world compiler and experiment engine remain in progress.

## System Entry Points
- `src/app/main.py`: FastAPI application factory (`create_app`).
- `src/app/cli/main.py`: Top-level CLI command dispatcher (`lab`).
- `src/app/cli/investigate.py`: Cloud sandbox investigation CLI (`lab investigate start/attach/send/list`).
- `src/app/cli/simulate.py`: User simulator CLI runner and scenario runner.
- `src/app/cli/textual_simulate.py`: Interactive full-screen Textual TUI simulator.
- `src/app/config.py`: Pydantic BaseSettings runtime configuration.
- `scripts/investigate_smoke.py`: Live Modal and Prime Agent investigation smoke test script.
- `alembic/env.py`: Database migration coordinator.

## Architecture & Design Patterns
- **Hexagonal / Clean Architecture:** Thin API and CLI delivery layers calling domain services with pluggable adapters.
- **Repository Pattern:** `SqlAlchemySupportRepository` and `SqlAlchemyInvestigationRepository` abstract SQL database interactions with PostgreSQL optimizations.
- **Sandbox Isolation Pattern:** `PostgresSupportSandbox` uses session-level `pg_temp` tables and transaction rollbacks for zero-state leakage.
- **Cloud Runner & Bridge Pattern:** Provider-agnostic `CloudRunner` protocol with `ModalRunner` Linux sandboxes, dynamic runner resolution (`ModalRunner` vs `FakeRunner`), automatic sandbox termination upon completion, the in-container `SandboxBridge` loop (task brief as the first prompt, inbox polling, batched event flushing, heartbeats, completion only after a valid summary and `agent_end`), `WorldGatewayService` exposing the nine investigation skill tools, and a Prime Agent TypeScript extension mapping those tools over loopback HTTP.
- **Investigation Lifecycle & Brief Assembly:** Finite state machine managing investigation status (`pending` -> `provisioning` -> `running` -> `completed`/`failed`/`cancelled`), structured markdown task brief rendering (`brief.py`), token-guarded event ingestion, interactive user steering, heartbeat silence sweeping, and SSE streaming of persisted events to reconnect clients.
- **State Machine / Flow Registry:** Multi-turn conversation workflows registered as `FlowPlugin` instances.
- **Privacy Allowlist / Denylist:** Zero-credential bundle compiler scrubbing sensitive tokens and PII before artifact emission.

## Directory Map (Aggregated)

| Directory | Responsibility Summary | Detailed Map |
|:---|:---|:---|
| [`src/app/`](file:///Users/king/Desktop/simulate/src/app/codemap.md) | Application factory, dependency injection, configuration, and database connection lifecycle. | [View Map](src/app/codemap.md) |
| [`src/app/api/`](file:///Users/king/Desktop/simulate/src/app/api/codemap.md) | Thin FastAPI route handlers for cases, runs, evaluations, investigations, and evidence. | [View Map](src/app/api/codemap.md) |
| [`src/app/cli/`](file:///Users/king/Desktop/simulate/src/app/cli/codemap.md) | CLI tools (`lab simulate`, `lab investigate`, etc.) and interactive Textual TUI for simulation runs, investigation management, and preflight checks. | [View Map](src/app/cli/codemap.md) |
| [`src/app/adapters/`](file:///Users/king/Desktop/simulate/src/app/adapters/codemap.md) | External integration adapters (Pydantic AI, LangSmith, LLM providers). | [View Map](src/app/adapters/codemap.md) |
| [`src/app/telemetry/`](file:///Users/king/Desktop/simulate/src/app/telemetry/codemap.md) | OpenTelemetry instrumentation, span processors, and sensitive data sanitization. | [View Map](src/app/telemetry/codemap.md) |
| [`src/app/domain/agent/`](file:///Users/king/Desktop/simulate/src/app/domain/agent/codemap.md) | Support agent execution service, prompt assembly, and policy constraints. | [View Map](src/app/domain/agent/codemap.md) |
| [`src/app/domain/agent_runner/`](file:///Users/king/Desktop/simulate/src/app/domain/agent_runner/codemap.md) | In-sandbox agent harness, Prime Agent RPC adapter, communication bridge, and World Gateway service. | [View Map](src/app/domain/agent_runner/codemap.md) |
| [`src/app/domain/audit/`](file:///Users/king/Desktop/simulate/src/app/domain/audit/codemap.md) | Telemetry scanning, audit event emission, and secret scrubbing. | [View Map](src/app/domain/audit/codemap.md) |
| [`src/app/domain/bundle/`](file:///Users/king/Desktop/simulate/src/app/domain/bundle/codemap.md) | Case bundle schemas, privacy allowlists, fixture validators, and packaging. | [View Map](src/app/domain/bundle/codemap.md) |
| [`src/app/domain/evidence/`](file:///Users/king/Desktop/simulate/src/app/domain/evidence/codemap.md) | Failure evidence generation, evaluation metrics, and summary persistence. | [View Map](src/app/domain/evidence/codemap.md) |
| [`src/app/domain/execution/`](file:///Users/king/Desktop/simulate/src/app/domain/execution/codemap.md) | Core execution service, deterministic replay, and state coordination. | [View Map](src/app/domain/execution/codemap.md) |
| [`src/app/domain/failures/`](file:///Users/king/Desktop/simulate/src/app/domain/failures/codemap.md) | Failure taxonomy, error categorizers, and root cause detectors. | [View Map](src/app/domain/failures/codemap.md) |
| [`src/app/domain/investigation/`](file:///Users/king/Desktop/simulate/src/app/domain/investigation/codemap.md) | Investigation lifecycle state machine, task brief assembly, cloud runner resolution, event stream ingestion, chat inbox, findings persistence, and sandbox termination. | [View Map](src/app/domain/investigation/codemap.md) |
| [`src/app/domain/regression/`](file:///Users/king/Desktop/simulate/src/app/domain/regression/codemap.md) | Golden regression datasets and regression test case persistence. | [View Map](src/app/domain/regression/codemap.md) |
| [`src/app/domain/retrieval/`](file:///Users/king/Desktop/simulate/src/app/domain/retrieval/codemap.md) | Policy retrieval engine, similarity matching, and document chunking. | [View Map](src/app/domain/retrieval/codemap.md) |
| [`src/app/domain/runner/`](file:///Users/king/Desktop/simulate/src/app/domain/runner/codemap.md) | Cloud runner interfaces, schemas, and Modal provider implementations for sandbox provisioning. | [View Map](src/app/domain/runner/codemap.md) |
| [`src/app/domain/simulation/`](file:///Users/king/Desktop/simulate/src/app/domain/simulation/codemap.md) | Sandbox provisioning, state machine transitions, and database rollbacks. | [View Map](src/app/domain/simulation/codemap.md) |
| [`src/app/domain/suite/`](file:///Users/king/Desktop/simulate/src/app/domain/suite/codemap.md) | Test suite storage, batch simulation execution, and test groupings. | [View Map](src/app/domain/suite/codemap.md) |
| [`src/app/domain/support/`](file:///Users/king/Desktop/simulate/src/app/domain/support/codemap.md) | Support entities (Customer, Order, Ticket, Policy) and SQL repositories. | [View Map](src/app/domain/support/codemap.md) |
| [`src/app/domain/user_simulator/`](file:///Users/king/Desktop/simulate/src/app/domain/user_simulator/codemap.md) | Multi-turn user simulation engine, persona scripting, and preflight checks. | [View Map](src/app/domain/user_simulator/codemap.md) |
| [`src/app/domain/workflow/`](file:///Users/king/Desktop/simulate/src/app/domain/workflow/codemap.md) | LangGraph workflow state graphs, checkpoints, and execution loops. | [View Map](src/app/domain/workflow/codemap.md) |
| [`src/app/domain/reference_workflows/`](file:///Users/king/Desktop/simulate/src/app/domain/reference_workflows/codemap.md) | Reference domain workflows (CI triage, claims denial, incident response). | [View Map](src/app/domain/reference_workflows/codemap.md) |
| [`alembic/`](file:///Users/king/Desktop/simulate/alembic/codemap.md) | Database migration scripts and schema versioning. | [View Map](alembic/codemap.md) |
