# `src/app/domain/simulation/`

## Responsibility

Runs one reviewed bundle through the real support agent and services inside a disposable
environment, while enforcing dependency coverage, fault boundaries, state isolation, safe event
capture, deterministic evaluation, and rollback cleanup.

## Design

- `SimulationScenario` separates approved expected behavior from original production behavior
  and pins state, actions, versions, budgets, transitions, dependency coverage, and provenance.
- `DependencyAdapter` and `EnvironmentProvisioner` are boundary contracts. A registry routes
  tools without ambiguity and reports coverage by dependency, adapter kind, and tool.
- `PostgresSandboxProvisioner` uses one transaction on a verified non-production target, real
  support repository/service operations, mutation observation, and rollback on destroy.
- Recorded adapters replay sanitized exact fixtures; `StatefulSupportAdapter` supplies an
  in-memory equivalent.
- `FaultInjectingRepository` injects declared failures at the owned database boundary before
  otherwise calling the real path.

## Flow

1. `run_bundle()` validates identity, reconstructs the scenario/state, and rejects fixtures or
   fault scripts that the reference agent cannot reach.
2. A provisioner creates the isolated environment. The runner checks coverage, seeds approved
   state, and connects `PydanticAISupportAgent` to the sandbox repository.
3. A trace listener maps model, tool, retry, database, and retrieval spans to allowlisted events
   and accumulates latency, token, cost, retry, tool, and grounding metrics.
4. After execution, the provisioner captures final state and exact mutations. Deterministic
   evaluators run, then the runner assigns `reproduced`, `accepted`, `failed`,
   `unexpected_access`, or `missing_coverage`.
5. `destroy()` always runs in `finally`. Cleanup failure becomes `CleanupRunError` or is
   attached to the original exception.
6. `SimulationEventCollector` keeps the complete transcript and bounded subscriber queues; slow
   subscribers drop their oldest queued event.

## Key Files

- `runner.py`: Reconstruction, execution, metrics, evaluators, verdict, and cleanup.
- `provisioner.py` / `postgres.py`: Provisioner contract and rollback sandbox.
- `adapters.py`: Adapter protocol, argument normalization, registry, and coverage.
- `stateful.py` / `recorded.py`: In-memory stateful and recorded adapters.
- `faults.py`: Fault scripts and repository wrapper.
- `events.py`: Transcript schema, collector, and live stream.
- `schemas.py` / `scenarios.py`: Scenario contracts and fixed scenarios.

## Integration

- Bundles come from `app.domain.bundle`; runs use the real agent adapter, support domain, and
  telemetry recorder.
- Execution/suite services, Model Lab, run APIs, and CLI commands call `run_bundle()`.
- `user_simulator.run_support()` reuses bundle compilation, PostgreSQL provisioning, mutations,
  and simulation events. Reference workflows reuse fault and event contracts.
