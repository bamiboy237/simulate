# Operational and Development Scripts

This directory contains utility scripts for database seeding, manual agent testing, and cloud sandbox smoke verification.

## Available scripts

### 1. Database seed (`scripts/seed.py`)

Populates the configured PostgreSQL database with deterministic customer, order, policy, and ticket fixtures.

To seed the database, run:

```bash
uv run python scripts/seed.py
```

### 2. Manual agent runner (`scripts/manual_agent.py`)

Runs a single support agent turn against an in-memory repository using a live hosted model.

To run a manual agent turn, set your model credentials and select a prompt scenario:

```bash
export MODEL_PROVIDER=openai
export MODEL_NAME=gpt-5.6-luna
export MODEL_API_KEY=<your-api-key>

uv run python scripts/manual_agent.py order-status
uv run python scripts/manual_agent.py refund
uv run python scripts/manual_agent.py refund-confirmed
```

### 3. Modal sandbox smoke test (`scripts/modal_smoke.py`)

Verifies Modal sandbox container creation, status polling, and termination.

To run the Modal container smoke test, configure your local Modal credentials and run:

```bash
uv run python scripts/modal_smoke.py
```

### 4. Investigation runner smoke test (`scripts/investigate_smoke.py`)

Executes an end-to-end live smoke test of the Modal investigation runner and Prime Agent workflow.

To run the investigation smoke test, set the live test flag and execute the script:

```bash
SIMULATE_LIVE_E2E=1 uv run python scripts/investigate_smoke.py
```

If `SIMULATE_LIVE_E2E` is unset or Modal credentials are missing, the script skips execution cleanly.
