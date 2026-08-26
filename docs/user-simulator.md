# User Simulator

The user simulator runs interactive personas against agent workflows and streams execution events to your terminal.

## CLI commands

| Command | Action |
| --- | --- |
| `uv run lab simulate` | Open the interactive full-screen Textual workbench. |
| `uv run lab simulate list` | List all available simulation scenarios. |
| `uv run lab simulate validate` | Validate simulation YAML manifests. |
| `uv run lab simulate run <scenario-id>` | Run a single scenario with live terminal event streaming. |
| `uv run lab simulate run <scenario-id> --yes` | Run without interactive prompts using default settings. |
| `uv run lab simulate run <scenario-id> --no-live` | Print plain text output line-by-line. |
| `uv run lab simulate run <scenario-id> --json` | Output events as JSON lines. |

To cancel an active run, press `Ctrl-C`. The runner rolls back database transactions, writes a final report with status `cancelled`, and exits.

---

## Configuration and catalogs

The user simulator uses three configuration sources:

1. **Scenario catalogs (`simulations/*.yaml`):** Define scenario metadata, initial user goals, default turn counts, and environment profiles.
2. **Environment profiles (`config/simulation-environments.yaml`):** Map non-secret environment requirements, such as database ports and test hosts.
3. **Flow plugins (`src/app/domain/user_simulator/plugins.py`):** Register the Python handlers that execute the workflow.

Secret values (such as API keys) resolve from local environment variables at runtime. They are never written to YAML files.

---

## Preflight checks

Before allocating a run ID or starting execution, the simulator verifies that:

- The requested plugin is registered in the plugin registry.
- The environment variable `ENVIRONMENT=test` is set.
- All required environment variables listed in the profile are set.
- The target PostgreSQL database is reachable on loopback (`127.0.0.1`) and database migrations are up to date.
- The artifact output directory is writable.

If any check fails, the CLI prints the missing requirement and stops execution.

---

## Persisted data and privacy

The simulator writes an append-only JSONL log and a final summary JSON report:

- **Saved data:** Turn numbers, selected tool names, outcomes, token usage, latency, retry counts, and final state diffs.
- **Excluded data:** Conversation text and raw tool argument values remain in memory during the run and are never written to disk.

---

## Example: Run a local simulation

To run a simulation against the local test database, set your environment variables and execute the scenario:

```bash
export ENVIRONMENT=test
export LAB_TEST_PG_URL=postgresql://postgres:postgres@127.0.0.1:55433/lab
export MODEL_PROVIDER=openai
export MODEL_NAME=gpt-5.6-luna
export MODEL_API_KEY=<your-model-api-key>

uv run lab simulate run phase2-03-database-timeout --max-turns 8
```
