# src/app/cli/

## Responsibility
Command-line interface tools providing commands for running user simulations, orchestrating cloud sandbox investigations, executing preflight checks, and driving an interactive terminal user interface.

## Key Files
- `main.py`: Top-level CLI command registry (`lab`) dispatching subcommands for simulation, investigation, and reporting.
- `investigate.py`: CLI subcommands (`lab investigate start`, `attach`, `send`, `list`) for launching investigations, streaming real-time events, sending steering prompts, and inspecting run status.
- `simulate.py`: Headless simulation runner (`lab simulate`) and scenario preflight checks.
- `textual_simulate.py`: Full-screen interactive Textual TUI for inspecting multi-turn simulation timelines, tool calls, and state transitions.
- `offline.py`: CLI runner for offline dataset evaluation and batch execution.
- `report.py`: CLI utility for generating formatted markdown and terminal reports.

## CLI Commands & Subcommands
- `lab investigate start <trace-ref> [--follow] [--world-id ...] [--slice-name ...]`: Starts a new investigation from a trace ID or JSON trace reference, generates a bridge token, launches a sandbox, and optionally streams events.
- `lab investigate attach <id> [--last-seq N] [--json]`: Connects to an existing investigation event stream and tails events until terminal status.
- `lab investigate send <id> "<message>" [--steer] [--json]`: Posts a prompt or steering message to an active investigation inbox.
- `lab investigate list [--limit N] [--json]`: Lists recent investigations in a rich terminal table or formatted JSON.
- `lab simulate ...`: Runs headless simulations and scenario validations.

## Design Patterns
- **Command Dispatcher / Subparsers Pattern:** `main.py` registers subcommands via `argparse` subparsers (`build_investigate_parser`), routing execution to async action handlers.
- **Async Terminal Streaming Pattern:** `_stream_events` polls the event stream via sequence cursors, formatting structured live events to console and terminating on final lifecycle status.
- **Rich Terminal Formatting:** Uses `rich.console.Console` and `rich.table.Table` for clear, colorized terminal reporting.

## Data & Control Flow
1. **Start Investigation (`_run_start`):** Parses trace and slice flags, builds `InvestigationCreateRequest` with rendered task brief (`render_task_brief`), calls `InvestigationService.start()`, commits session, and prints ID and token. If `--follow` is set, begins event tailing.
2. **Attach Event Stream (`_run_attach`):** Connects to `service.get_events_stream()` from specified sequence cursor (`--last-seq`), streaming events until reaching a terminal status (`completed`, `failed`, `cancelled`).
3. **Send Steering Message (`_run_send`):** Encodes user prompt or steering instruction (`--steer`) and invokes `service.record_user_message()`.
4. **List Investigations (`_run_list`):** Queries `service.list_investigations()` and renders summary rows into a Rich table.

## Integration
- **Consumed by:** Terminal operators, triage engineers, local developers, CI/CD automation scripts.
- **Depends on:** `app.domain.investigation` (`InvestigationService`, `SqlAlchemyInvestigationRepository`, `render_task_brief`), `app.domain.runner.schemas`, `app.domain.user_simulator`, `app.config`, `app.db`, `rich`.
