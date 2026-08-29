# src/app/domain/

## Responsibility
Core business logic layer containing domain models, policies, state machines, and business services.

## Sub-Domains
- `agent/`: Support agent business logic.
- `agent_runner/`: In-sandbox harness, communication bridge, Prime Agent RPC adapter, and World Gateway service.
- `audit/`: Audit trails and telemetry scanning.
- `bundle/`: Case bundle packaging and allowlist verification.
- `comparison/`: Baseline/candidate configuration experiments and objective comparison gates.
- `evidence/`: Failure evidence and verification summaries.
- `execution/`: Execution orchestrator and replay coordinator.
- `failures/`: Failure taxonomy and classification.
- `investigation/`: Investigation domain service, lifecycle state machine, task brief assembly, runner resolution, event collection, chat inbox, findings persistence, and sandbox termination.
- `reference/`: Reference workflow contracts, runners, comparisons, and reports.
- `reference_workflows/`: CI triage, claims denial, and incident response fixture workflows.
- `regression/`: Golden regression test sets.
- `retrieval/`: Knowledge retrieval and vector search.
- `runner/`: Cloud runner interfaces, schemas, and Modal provider implementations.
- `simulation/`: Sandbox environments and state transitions.
- `suite/`: Test suite storage and batch runs.
- `support/`: Customer support entities and SQL repositories.
- `user_simulator/`: Multi-turn persona dialogue simulator.
- `workflow/`: LangGraph state machine workflows.

## Cross-Domain Flow

Evidence imports feed failure grouping and reviewed bundles. Bundles feed isolated simulations,
suites, and baseline/candidate comparisons. Workflows and user simulations reuse support,
retrieval, agent, and telemetry services. Investigations use the runner contract to start a
sandbox, then use the agent bridge and persisted event stream to expose live evidence.

## Invariants

- Domain services own authorization, confirmation, state transitions, and isolation checks.
- Cloud runners are provider adapters behind `CloudRunner`; `ModalRunner` and `FakeRunner` are
  implementations.
- Simulate produces evidence. It does not deploy a candidate or mutate customer production state.
