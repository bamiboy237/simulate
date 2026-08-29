# `src/app/domain/user_simulator/`

## Responsibility

Runs live multi-turn synthetic-user conversations against built-in support and reference
workflows. It provides plugin/catalog loading, environment preflight, privacy-split events, safe
tool projection, hosted persona execution, reports, and cleanup.

## Design

- `FlowPlugin` is the generic async contract; `FlowRegistry` rejects duplicate IDs and keeps
  support/reference details out of the CLI.
- YAML manifests are untrusted configuration. Strict models, aggregated `CatalogIssue` values,
  exact plugin references, secret-key checks, and path validation build a `SimulationCatalog`.
  Callers list scenario IDs and environments, then resolve one environment by profile ID.
- Each event has a display view that may contain chat text but cannot serialize, and a persistent
  view with only allowlisted scalar operational fields. `JsonlPersistentSink` accepts typed
  `SimulationEvent` values and writes only their persistent view.
- `ToolProjectionRegistry` exposes only exact per-flow fields; unknown details stay hidden.
- `PersonaConversation` owns hosted persona turns, trusted confirmations, usage accounting,
  stop conditions, and the final `SimulatorReport`.

## Flow

1. The CLI loads scenario/environment YAML, resolves a plugin, and runs preflight before creating
   artifacts.
2. Preflight resolves only the selected profile's environment-variable names, checks artifact
   writes, probes required databases, and verifies the migration head.
3. `_FlowPlugin.run()` applies persona overrides and dispatches to `run_support()` or
   `run_reference()` with a `FlowRunRequest` and `RuntimeEnvironment`.
4. `build_emitter()` fans events to JSONL, display memory, and optional UI sinks. Renderer
   failures do not alter business execution.
5. Support compiles a fixed scenario, provisions a rollback PostgreSQL sandbox, and calls the
   real support agent. Reference flows seed registered in-memory workflows and real tool objects.
6. The conversation alternates persona/product turns, handles confirmation, verifies expected
   and permitted transitions, writes the report, and destroys or rolls back state in `finally`.

## Key Files

- `flows.py`: Generic flow/plugin/projector/registry contracts.
- `plugins.py`: Built-in plugin factories and safe tool projection.
- `manifests.py`: YAML schemas, validation, and catalog construction.
- `preflight.py`: Runtime, artifact, database, isolation, and migration checks.
- `events.py`: Privacy-split events, emitters, memory, and JSONL persistence.
- `simulator.py`: Persona engine and support/reference adapters.
- `personas.py` / `models.py`: Built-in personas and result contracts.

## Integration

- `app.cli.simulate` provides list/validate/run; `app.cli.textual_simulate` provides the
  interactive workbench; `app.cli.main` builds the default registry.
- Support reuses bundle/simulation packages, the real agent, settings, sessions, and scenarios.
- Reference flows use `app.domain.reference.workflows.six_reference`.
- Hosted execution uses the shared Pydantic AI model builder and is gated to test environments.
