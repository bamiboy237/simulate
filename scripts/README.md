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

**What it proves:** The investigation container image builds with Node.js 22 and Prime Agent v0.8.1 on PATH (the image build fails if `node --version`, `npm --version`, or `prime-agent --version` fails), and that sandbox creation, status polling, termination, and repeated (idempotent) termination work against the real Modal API.

**Prerequisites:** Modal credentials configured locally (`modal token set`). The script runs unconditionally; it does not skip when credentials are missing.

**Success:** Prints `=== Smoke test completed successfully ===` and returns exit code 0.

**Failure:** Prints the error to stderr and returns exit code 1 (for example, when Modal credentials are missing or the image build fails).

To run:

```bash
uv run python scripts/modal_smoke.py
```

### 4. Investigation runner smoke test (`scripts/investigate_smoke.py`)

**What it proves:** One live investigation end to end against real Modal: the sandbox boots the bridge and Prime Agent in RPC mode, the agent processes the task brief through the World Gateway tools, ordered events persist to PostgreSQL, exactly one summary persists, and the sandbox terminates after the terminal status.

**Prerequisites:**

- `SIMULATE_LIVE_E2E=1` and `MODAL_ENABLED=true`.
- Modal credentials (the script uses `ModalRunner` directly).
- PostgreSQL reachable at `DATABASE_URL`; the script persists the investigation and its events.
- `CONTROL_PLANE_PUBLIC_URL`: a public HTTPS URL reaching this control plane from the cloud sandbox (the verified run used an HTTPS tunnel).
- `MODAL_OUTBOUND_DOMAIN_ALLOWLIST`: bare domain names that must include the control-plane host and the model-provider host (for example `api.openai.com`).
- Model credentials for a standard provider: `MODEL_PROVIDER=openai` with `MODEL_API_KEY` (or Anthropic). The service maps them into the sandbox as `OPENAI_API_KEY` or `ANTHROPIC_API_KEY`. Prime Agent v0.8.1 requires a custom `models.json` for non-standard OpenAI-compatible providers, so custom `MODEL_BASE_URL` endpoints are not covered by this smoke test.

The script's 300s sandbox uses a 120s tool timeout and 60s recovery timeout (it sets `TOOL_TIMEOUT_S=120` and `TOOL_RECOVERY_TIMEOUT_S=60` defaults before loading settings) and injects the outer lifetime as `SANDBOX_TIMEOUT_S`. The bridge watchdog fires on the first of per-tool age or the overall run cutoff (`SANDBOX_TIMEOUT_S` − recovery − reserve), so even a tool that starts mid-run is aborted before Modal kills the container; the watchdog budget is validated against every sandbox's outer timeout at launch, and a violating configuration is rejected before any Modal container starts.

**Success:** Prints `PASS investigation=<id>: completed with summary` and returns exit code 0.

**Skip:** Returns exit code 0 without doing any work when `SIMULATE_LIVE_E2E` is unset or `MODAL_ENABLED` is false.

**Failure:** Returns exit code 1 when the investigation times out without a terminal status, ends `failed` or `cancelled`, completes without a persisted summary, has no sandbox handle, or leaves the sandbox running after the terminal status.

The script prints the investigation ID on success and on failure, and never prints the bridge token or any model credential.

To run:

```bash
SIMULATE_LIVE_E2E=1 MODAL_ENABLED=true uv run python scripts/investigate_smoke.py
```
