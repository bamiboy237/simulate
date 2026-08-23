# src/app/domain/runner/

## Responsibility
Cloud Sandbox Infrastructure Layer. Defines the contracts, schemas, and provider implementations for provisioning, monitoring, and terminating isolated cloud compute sandbox environments (Phase 8 MVP powered by Modal) for agent investigations and reproduction slices.

## Design Patterns
- **Protocol / Strategy Pattern:** `CloudRunner` runtime checkable protocol defines a provider-agnostic interface (`create_sandbox`, `get_status`, `terminate`) decoupling control plane orchestration from specific cloud providers (Modal, Fly, E2B, etc.).
- **Adapter / Gateway Pattern:** `ModalRunner` and low-level `_modal_client.py` isolate the third-party Modal SDK dependencies behind a mockable Python boundary for offline unit testing.
- **Data Transfer Object / Value Object Pattern:** `SandboxSpec`, `SandboxHandle`, `RunnerStatus`, `RunnerEvent`, and `ResourceLimits` provide strict Pydantic model validation (`extra="forbid"`) and secret shielding (`SecretStr`).

## Key Files
- `base.py`: Defines the `CloudRunner` protocol.
- `schemas.py`: Core Pydantic data schemas, event types (`EventType`), and runner statuses (`RunnerState`, `RunnerStatus`).
- `modal_runner.py`: `ModalRunner` implementing `CloudRunner` using Modal Linux containers.
- `_modal_client.py`: Low-level Modal SDK wrapper isolating image building and container APIs.

## Data & Control Flow
1. Caller (e.g. Investigation lifecycle orchestrator or API runner) creates a validated `SandboxSpec` containing task briefs, environment slice refs, agent artifact digests, resource limits, and secrets.
2. Caller invokes `CloudRunner.create_sandbox(spec)` on `ModalRunner`.
3. `ModalRunner` maps the spec parameters to environment variables and container secrets, builds the Debian slim investigation image recipe via `_modal_client.build_investigation_image()`, and launches a detached container running the bridge command (`python -m app.domain.agent_runner.bridge`).
4. A `SandboxHandle` with provider metadata and sandbox ID is returned.
5. The control plane polls container health via `ModalRunner.get_status(handle)` or terminates execution via `ModalRunner.terminate(handle)`.

## Integration
- **Consumed by:** `app.domain.investigation` (investigation lifecycle management), `app.api.investigations` / runner endpoints, test runners.
- **Depends on:** `_modal_client` (wrapping Modal SDK), `pydantic` (BaseModel, SecretStr).
