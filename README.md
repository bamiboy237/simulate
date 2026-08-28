# Simulate

Simulate creates isolated, resettable sandbox environments to test, investigate, and improve AI agents against realistic business workflows.

Simulate runs customer agents inside disposable sandboxes with sanitized database state and mock services. Teams use Simulate to reproduce production failures, test agent updates, and measure behavioral changes before releasing code to production. Simulate does not host customer agents in production.

## Current status

- **Phases 0 to 7 (Complete):** Core support domain, LangSmith and Braintrust trace ingestion, LangGraph stateful workflow checkpointing, isolated PostgreSQL provisioning, and the terminal user simulator.
- **Phase 8 MVP (Complete):** Runs Prime Agent (a coding harness by Prime Intellect) inside a detached Modal cloud sandbox. Prime Agent investigates production traces, reproduces issues, streams live events, supports two-way chat during execution, and writes an immutable evidence summary. Merged to `main` in `7b3c01a` (August 23, 2026) and verified live against a real Modal sandbox on August 26, 2026 (see [Verified cloud investigation workflow](#verified-cloud-investigation-workflow)).
- **Phase 8 full (In progress):** The business world compiler and controlled experiment engine (roadmap sub-phases 8.0 through 8.8) remain in progress. See [`BUILD_ROADMAP.md`](file:///Users/king/Desktop/simulate/BUILD_ROADMAP.md).

For the milestone plan and system architecture, see [`BUILD_ROADMAP.md`](file:///Users/king/Desktop/simulate/BUILD_ROADMAP.md) and [`ARCHITECTURE.md`](file:///Users/king/Desktop/simulate/ARCHITECTURE.md).

## Requirements

- Python 3.12 or newer
- [`uv`](https://docs.astral.sh/uv/)
- PostgreSQL (local or Neon)
- Docker (optional, for containerized local development)

## Quickstart

### 1. Configure the environment

If you use Neon, pull your development configuration:

```bash
neon env pull --file .env
```

If you do not use Neon, copy the example environment file and set your credentials:

```bash
cp .env.example .env
```

### 2. Install dependencies and apply migrations

To install dependencies and migrate the database, run:

```bash
uv sync --frozen
uv run alembic upgrade head
```

To load sample fixture data for local testing, run:

```bash
uv run python scripts/seed.py
```

### 3. Start the API server

To start the local FastAPI application, run:

```bash
uv run uvicorn app.main:create_app --factory
```

To verify that the server is healthy, run:

```bash
curl -i http://127.0.0.1:8000/healthz
curl -i http://127.0.0.1:8000/readyz
```

---

## Docker development

You can run PostgreSQL and the API server together with Docker Compose.

To build and start the containers, run:

```bash
docker compose up --build
```

The database binds to `127.0.0.1:55433` and persists data in a named Docker volume.

To run the test suite inside the Docker container, run:

```bash
docker compose --profile test run --rm test
```

To stop all containers and remove the database volume, run:

```bash
docker compose down -v
```

---

## Command-line workflows

### User simulator

The user simulator runs persona-driven test scenarios against target agents and streams events to your terminal.

To open the full-screen terminal interface:

```bash
uv run lab simulate
```

To list available simulation scenarios:

```bash
uv run lab simulate list
```

To run a specific scenario and view the live event stream:

```bash
uv run lab simulate run reference-disputes
```

To output plain text or JSON lines instead of the interactive UI:

```bash
uv run lab simulate run reference-disputes --no-live
uv run lab simulate run reference-disputes --json
```

For configuration details and privacy rules, see [`docs/user-simulator.md`](file:///Users/king/Desktop/simulate/docs/user-simulator.md).

### Cloud investigations

The investigation workflow runs an agent inside a detached Modal cloud sandbox to debug production traces.

To start an investigation from a trace ID:

```bash
uv run lab investigate start <trace-id> --follow
```

To list recent investigations:

```bash
uv run lab investigate list
```

To attach to a running investigation stream:

```bash
uv run lab investigate attach <investigation-id>
```

To send a message or steer the investigator agent during execution:

```bash
uv run lab investigate send <investigation-id> "Check the payment gateway timeout." --steer
```

### Verified cloud investigation workflow

Two smoke scripts prove the cloud investigation path against real Modal infrastructure. The prerequisites, exact commands, and success, skip, and failure behavior for each script are documented in [`scripts/README.md`](file:///Users/king/Desktop/simulate/scripts/README.md).

```bash
uv run python scripts/modal_smoke.py
SIMULATE_LIVE_E2E=1 MODAL_ENABLED=true uv run python scripts/investigate_smoke.py
```

`scripts/modal_smoke.py` proves that the investigation image builds with Node.js 22 and Prime Agent v0.8.1 on PATH, and that sandbox creation, status polling, and idempotent termination work. `scripts/investigate_smoke.py` runs one investigation end to end: a real Modal sandbox boots the bridge and Prime Agent, reaches the public control plane through an HTTPS tunnel, exercises the World Gateway tools, persists ordered events, writes exactly one summary, and terminates cleanly.

The verified live path uses standard OpenAI or Anthropic credentials mapped to `OPENAI_API_KEY` or `ANTHROPIC_API_KEY`. Prime Agent v0.8.1 requires a custom `models.json` for non-standard OpenAI-compatible providers, so custom `MODEL_BASE_URL` endpoints are not covered by this workflow.

---

## Quality checks

Run these checks before you push changes or open a pull request:

```bash
uv run ruff check .
uv run mypy src
uv run pytest tests/unit -q
uv run pytest
```

---

## Documentation

- [`ARCHITECTURE.md`](file:///Users/king/Desktop/simulate/ARCHITECTURE.md): System architecture, control plane versus execution plane, and sandbox lifecycle.
- [`BUILD_ROADMAP.md`](file:///Users/king/Desktop/simulate/BUILD_ROADMAP.md): Product roadmap, milestone acceptance criteria, and schema contracts.
- [`AGENTS.md`](file:///Users/king/Desktop/simulate/AGENTS.md): Coding style, testing rules, and commands for autonomous agents.
- [`docs/user-simulator.md`](file:///Users/king/Desktop/simulate/docs/user-simulator.md): Simulator setup, preflight checks, and event contracts.
- [`docs/reference_workflows/README.md`](file:///Users/king/Desktop/simulate/docs/reference_workflows/README.md): Reference business workflow designs and offline fixtures.
