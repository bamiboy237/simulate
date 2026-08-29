# `src/app/cli/`

## Responsibility

This directory is the operator-facing command-line delivery layer for the `lab` executable.
It exposes evidence import, bundle and regression-library operations, isolated runs and
comparisons, proof and audit workflows, generic user simulations, reference workflows, and
cloud investigations. Commands call the same domain services and runners as the HTTP layer.
Human output is concise; `--json` provides stable machine output.

## File Map

| File | Responsibility |
|---|---|
| `main.py` | Top-level `argparse` registry, shared settings/error/output helpers, and handlers for evidence, bundles, cases, suites, comparisons, proof, audit, reference, simulator, and investigation groups. |
| `investigate.py` | `lab investigate start`, `attach`, `send`, and `list`; builds requests, persists messages, and tails event sequences to a terminal state. |
| `simulate.py` | Generic `lab simulate` command, catalog selection, setup review, preflight, runtime resolution, Rich/plain/JSON event sinks, interrupt reporting, and Textual dispatch. |
| `textual_simulate.py` | Optional full-screen setup and run viewer over the same `FlowPlugin` and `SimulationEvent` contracts. It does not own domain execution or persistence. |
| `offline.py` | Deterministic `FunctionModel` plans and fault scripts that replace only the hosted-model boundary. |
| `report.py` | Versioned Pydantic schema for the eight-scenario proof report. |
| `__init__.py` | Package marker. |

## Command Surface

| Command | Domain path |
|---|---|
| `lab import-trace` | Source adapter -> `TraceImportService` -> `SqlAlchemyEvidenceStore` |
| `lab scenario create` | Fixture trace source -> `scenario_with_evidence()` |
| `lab bundle compile` | Reviewed input + scenario/evidence -> `compile_bundle()` -> JSON artifact |
| `lab run` | Bundle file or `RegressionCaseService.get_case()` -> `run_bundle()` with an isolated PostgreSQL provisioner |
| `lab compare` | Bundles + one validated model change -> `run_model_lab()` |
| `lab regression add` / `lab cases list` | `RegressionCaseService` over `SqlAlchemyRegressionCaseRepository` |
| `lab suite create` / `lab suites list` | `SuiteService` over suite and case repositories |
| `lab suite run` | Resolve suite and exact case versions -> `run_suite_comparison()` |
| `lab proof eight` | Compile/save eight cases -> save suite -> `run_cohort_model_comparison()` -> proof artifacts |
| `lab audit run` | Privacy scanners + repeated isolated offline runs + persistence/transaction checks -> audit artifacts |
| `lab reference run` / `report` | `run_reference_case()` -> `compare_reference_runs()`; report covers all reference workflows |
| `lab simulate [run]`, `list`, `validate` | YAML catalog + default `FlowRegistry` -> preflight/runtime -> selected `FlowPlugin.run()` |
| `lab investigate start`, `attach`, `send`, `list` | `InvestigationService` over `SqlAlchemyInvestigationRepository` |

## Design Patterns

- **Command Dispatcher:** `main.build_parser()` composes nested parsers and stores handlers with
  `set_defaults`; `main()` invokes the selected handler.
- **Shared Service Boundary:** Commands create the same repository-backed domain services used
  by FastAPI. The CLI owns input/output and transaction scope, not business policy.
- **Dual Output:** `emit()` and command renderers select human output or JSON. Command handlers
  call `fail()` for expected failures, and `main()` converts unexpected failures to safe errors.
- **Strategy/Plugin:** The generic simulator selects a registered `FlowPlugin` from YAML
  metadata. Support and reference details stay in `app.domain.user_simulator.plugins`.
- **Preflight Gate:** Simulation artifacts are created only after catalog, environment, model,
  database, isolation, migration, and artifact-path checks pass.
- **Observer/Event Sink:** A flow emits `SimulationEvent` values to Rich, plain, silent JSON, or
  Textual sinks without changing execution.
- **Deterministic Test Seam:** `offline.py` replaces the hosted-model factory with scripted
  behavior while retaining real simulation, persistence, fault, and cleanup paths.
- **Cursor-Based Investigation Tail:** Attach/follow tracks the latest sequence, fetches newer
  persisted events, and stops when the service reports a terminal state.

## Data and Control Flow

### Top-level dispatch

1. `main.build_parser()` registers core commands and delegates simulator and investigation
   parser construction.
2. The selected synchronous handler uses `asyncio.run()` around async transactions or runners.
3. Settings, model configuration, bundle files, and case/suite references are validated before
   execution. `_provisioner_factory()` refuses non-test sandbox targets.
4. The handler calls a domain service or runner, then prints human output or JSON. Errors leave
   through the stable `lab: error [...]` boundary.

### Evidence-to-regression workflow

1. `import-trace` fetches fixture or LangSmith evidence and persists it through
   `TraceImportService`.
2. `scenario create` binds a scenario to evidence. `bundle compile` requires reviewed input
   and produces an approved content-addressed `SimulationBundle`.
3. `regression add` sends the bundle to `RegressionCaseService`, which validates the review
   and hash and saves an immutable case version.
4. `suite create` validates every exact case version through `SuiteService` and saves an
   immutable suite version.
5. `run`, `compare`, and `suite run` resolve saved inputs and enter isolated simulation or
   comparison runners.

### Proof, audit, and reference workflows

1. `proof eight` compiles the eight registered scenarios, saves cases and a suite, runs
   baseline/candidate cohort comparison, and writes `ProofReport` JSON and Markdown.
2. `audit run` scans persisted bundles and reports, repeats an isolated deterministic run,
   checks stable results and unchanged support tables, then records facts, risks, and skipped
   checks.
3. `reference run` compares one reference baseline and candidate. `reference report` repeats
   the flow across `ALL_WORKFLOWS` and records reuse, capability gaps, and verification notes.

### Generic user simulator

1. `build_default_registry()` registers built-in support and reference plugins.
2. `load_simulation_catalog()` loads strict scenario YAML and non-secret environment profiles;
   `list` renders the catalog and `validate` reports safe validation issues.
3. `simulate run` selects a scenario through flags, prompts, or Textual setup, applies
   persona/script/goal/turn overrides, and shows the resolved setup.
4. `run_preflight()` checks the plugin, variables, model, disposable database, migrations,
   isolation, and artifacts. `resolve_runtime()` produces the runtime passed to the plugin.
5. `FlowPlugin.run()` writes JSONL and a final report while emitting display-safe events.
   Textual adds setup, navigation, filtering, pause/view state, metrics, and evidence views.
6. On `Ctrl-C`, `simulate.py` reads the persistent cleanup event and reports only the cleanup
   status it observed.

### Cloud investigation workflow

1. `investigate start` accepts a trace ID or JSON reference, builds an
   `EnvironmentSliceRef`, Prime investigator reference, task brief, and resource limits, then
   calls `InvestigationService.start()` and commits.
2. `--follow` and `investigate attach` call `get_events_stream()` with a sequence cursor,
   render each event, and poll `get_by_id()` until completed, failed, or cancelled.
3. `investigate send` stores a prompt or steer message through
   `InvestigationService.record_user_message()` for the sandbox bridge.
4. `investigate list` calls `list_investigations()` and renders JSON or a Rich table.

## Integration Points

- **Executable entry:** `app.cli.main:main` (`lab`).
- **Configuration and persistence:** `app.config`, `app.db`, SQLAlchemy repositories, and
  fixture or LangSmith evidence sources.
- **Simulation and comparison:** `app.domain.simulation`, `app.domain.comparison`,
  `app.domain.suite.runner`, and PostgreSQL provisioners.
- **User simulator:** `app.domain.user_simulator` flow, manifest, preflight, plugin, event, and
  runtime contracts.
- **Investigations:** `app.domain.investigation` plus `app.domain.runner` schemas.
- **Presentation:** `argparse`, Rich, Textual, JSON, Markdown, and append-only JSONL artifacts.
