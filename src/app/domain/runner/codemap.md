# src/app/domain/runner/

## Responsibility
Cloud Sandbox Infrastructure Layer. Defines the contracts, schemas, and provider implementations for provisioning, monitoring, and terminating isolated cloud compute sandbox environments (Phase 8 MVP powered by Modal) for agent investigations and reproduction slices.

## Design Patterns
- **Protocol / Strategy Pattern:** `CloudRunner` runtime checkable protocol defines a provider-agnostic interface (`create_sandbox`, `get_status`, `terminate`) decoupling control plane orchestration from specific cloud providers (Modal, Fly, E2B, etc.).
- **Adapter / Gateway Pattern:** `ModalRunner` and low-level `_modal_client.py` isolate the third-party Modal SDK dependencies behind a mockable Python boundary for offline unit testing.
- **Data Transfer Object / Value Object Pattern:** `SandboxSpec`, `SandboxHandle`, `RunnerStatus`, `RunnerEvent`, and `ResourceLimits` provide strict Pydantic model validation (`extra="forbid"`) and secret shielding (`SecretStr`).

## Key Files
- `base.py`: Defines the `CloudRunner` protocol.
- `schemas.py`: Core Pydantic data schemas, event types (`EventType`), runner statuses (`RunnerState`, `RunnerStatus`), `SandboxSpec` with reserved env/secret key guards, the `outbound_domain_allowlist` field, and the resolved watchdog budget (`tool_timeout_s` / `tool_recovery_timeout_s`) validated to fit the sandbox outer timeout with a run reserve.
- `modal_runner.py`: `ModalRunner` implementing `CloudRunner` using Modal Linux containers.
- `_modal_client.py`: Low-level Modal SDK wrapper isolating image building and container APIs.

## Data & Control Flow
1. Caller (e.g. Investigation lifecycle orchestrator or API runner) creates a validated `SandboxSpec` containing the investigation ID, task brief, environment slice refs, agent artifact digests, resource limits, secrets, and the outbound domain allowlist.
2. Caller invokes `CloudRunner.create_sandbox(spec)` on `ModalRunner`.
3. `ModalRunner` maps the spec into a reserved set of environment variables (`INVESTIGATION_ID`, `TASK_BRIEF`, `CONTROL_PLANE_CALLBACK_URL`, `WORLD_GATEWAY_URL`, world/slice identities, `TRACE_REF` as JSON when a trace reference exists, `TOOL_TIMEOUT_S`/`TOOL_RECOVERY_TIMEOUT_S` for the bridge watchdog, and `SANDBOX_TIMEOUT_S` for the bridge's overall run cutoff; caller-supplied reserved keys are rejected), places `BRIDGE_TOKEN` in the Modal secret set, and launches a detached container running the bridge command (`python -m app.domain.agent_runner.bridge`).
4. `_modal_client.build_investigation_image()` builds the image recipe: Debian slim with Python 3.12, Node.js 22 (minimum 22.8.0) installed and version-checked, Prime Agent pinned to v0.8.1 (install fails if `prime-agent --version` fails), app code and the `world_gateway.ts` extension copied to `/root/.prime/agent/extensions/`.
5. When the allowlist is non-empty, `_modal_client.create_sandbox()` passes it to Modal's native sandbox network controls (`outbound_domain_allowlist`), so undeclared hosts are denied instead of using unrestricted egress.
6. A `SandboxHandle` with provider metadata and sandbox ID is returned.
7. The control plane polls container health via `ModalRunner.get_status(handle)` or terminates execution via `ModalRunner.terminate(handle)` (idempotent).

## Integration
- **Consumed by:** `app.domain.investigation` (investigation lifecycle management), `app.api.investigations` / runner endpoints, test runners.
- **Depends on:** `_modal_client` (wrapping Modal SDK), `pydantic` (BaseModel, SecretStr).
