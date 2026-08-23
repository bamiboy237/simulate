# src/app/domain/

## Responsibility
Core business logic layer containing domain models, policies, state machines, and business services.

## Sub-Domains
- `agent/`: Support agent business logic.
- `agent_runner/`: In-sandbox harness, communication bridge, Prime Agent RPC adapter, and World Gateway service.
- `audit/`: Audit trails and telemetry scanning.
- `bundle/`: Case bundle packaging and allowlist verification.
- `evidence/`: Failure evidence and verification summaries.
- `execution/`: Execution orchestrator and replay coordinator.
- `failures/`: Failure taxonomy and classification.
- `investigation/`: Investigation domain service, lifecycle state machine, task brief assembly, runner resolution, event collection, chat inbox, findings persistence, and sandbox termination.
- `regression/`: Golden regression test sets.
- `retrieval/`: Knowledge retrieval and vector search.
- `runner/`: Cloud runner interfaces, schemas, and Modal provider implementations.
- `simulation/`: Sandbox environments and state transitions.
- `suite/`: Test suite storage and batch runs.
- `support/`: Customer support entities and SQL repositories.
- `user_simulator/`: Multi-turn persona dialogue simulator.
- `workflow/`: LangGraph state machine workflows.
