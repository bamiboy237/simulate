# src/app/domain/runner/

## Responsibility

Defines the execution-provider boundary for Phase 8 investigations. The package owns the
provider-neutral sandbox contract, the validated data passed across that boundary, the Modal
adapter that launches the real container, and an in-memory fake used by offline callers.

## Design

- **Strategy / Protocol:** `CloudRunner` is a runtime-checkable protocol with three synchronous
  operations: `create_sandbox()`, `get_status()`, and idempotent `terminate()`.
- **Strict boundary objects:** `SandboxSpec`, `SandboxHandle`, `RunnerStatus`, `RunnerEvent`,
  `EnvironmentSliceRef`, `AgentArtifactRef`, and `ResourceLimits` forbid extra fields.
- **Reserved-key guard:** `SandboxSpec` rejects caller environment keys owned by the runner. The
  investigation service separately rejects caller attempts to supply `BRIDGE_TOKEN`.
- **Watchdog budget invariant:** `SandboxSpec` resolves default tool and recovery timeouts, then
  requires `tool_timeout_s + tool_recovery_timeout_s + 60 <= sandbox timeout`.
- **Adapter boundary:** `ModalRunner` converts `SandboxSpec` into Modal inputs.
  `_modal_client.py` contains every direct Modal SDK call.
- **Lazy provider import:** `runner.__getattr__()` imports `ModalRunner` only when requested.
- **Test double:** `FakeRunner` records specs and handles, supports injected failures and scripted
  status progressions, and makes termination observable without external compute.

## Key Files

- `base.py`: `CloudRunner` protocol.
- `schemas.py`: sandbox references, limits, lifecycle states, events, reserved keys, and watchdog
  validation.
- `modal_runner.py`: `SandboxSpec` to Modal-container adapter.
- `_modal_client.py`: Modal image recipe and SDK calls for create, poll, and terminate.
- `fake.py`: configurable in-memory `CloudRunner`.
- `__init__.py`: public exports and lazy `ModalRunner` loading.

## Data and Control Flow

1. `InvestigationService.start()` creates a `SandboxSpec`. Validation resolves watchdog values
   before an investigation record is inserted.
2. `ModalRunner.create_sandbox()` maps the spec to runner-owned environment variables for the
   investigation, task, world, slice, agents, trace reference, callback, and watchdog.
3. The adapter requires a non-empty `BRIDGE_TOKEN`, unwraps `SecretStr` values only for Modal
   secret injection, and selects the request allowlist or its configured default.
4. `_modal_client.create_sandbox()` looks up the Modal app, builds or receives an image, creates a
   detached sandbox, and returns its object ID. The command is
   `python -m app.domain.agent_runner.bridge`.
5. The default image uses Python 3.12, Node.js 22.8 or newer, Prime Agent 0.8.1, the local `app`
   source, and the World Gateway extension at Prime Agent's extension path.
6. `get_status()` maps a pending return code to `running`, exit code `0` to `completed`, and
   other codes to `failed`. An unreachable sandbox maps to exit code `-1`.
7. `terminate()` delegates to exception-swallowing Modal cleanup. `FakeRunner` implements the
   same contract in memory.
8. Runtime chat and event traffic bypasses `CloudRunner`; the bridge uses control-plane HTTP after
   provisioning.

## Integration

- **Primary consumer:** `app.domain.investigation.service.InvestigationService`.
- **Container consumer:** `app.domain.agent_runner.bridge`.
- **Operational consumer:** `scripts/modal_smoke.py`.
- **External dependency:** the `modal` package, isolated in `_modal_client.py`.
