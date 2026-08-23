# Operational and Development Scripts

This directory contains utility scripts for database seeding and manual agent testing.

## Available scripts

### 1. Database seed (`scripts/seed.py`)

Populates the configured PostgreSQL database with deterministic customer, order, policy, and ticket fixtures.

```bash
uv run python scripts/seed.py
```

### 2. Manual agent runner (`scripts/manual_agent.py`)

Runs a single support agent turn against an in-memory repository using a live hosted model.

```bash
export MODEL_PROVIDER=openai
export MODEL_NAME=gpt-5.6-luna
export MODEL_API_KEY=<your-api-key>

uv run python scripts/manual_agent.py order-status
uv run python scripts/manual_agent.py refund
uv run python scripts/manual_agent.py refund-confirmed
```

