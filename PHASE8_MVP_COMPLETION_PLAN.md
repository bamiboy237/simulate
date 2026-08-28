# Phase 8 MVP completion plan

Status: approved for implementation on 2026-08-26.

Owner: lil dipper implements each milestone. Big dipper reviews each milestone before the
next one starts. The user performs the final review before any push.

## Goal

Make the Phase 8 Modal investigation MVP match its accepted contract:

1. Start a detached Modal sandbox for one investigation.
2. Start the World Gateway and Prime Agent inside the sandbox.
3. Send the task brief through Prime Agent's RPC protocol.
4. Relay prompt, steer, and follow-up messages while the run is active.
5. Persist ordered events and a structured final summary.
6. Terminate the sandbox after completion or failure.
7. Enforce an explicit outbound network policy.
8. Update documentation and Linear only after the evidence supports these claims.

## Confirmed defects

These are observed facts, not hypotheses:

- `SandboxBridge.run()` emits `investigation_started` and exits. It does not run its helper
  methods as a bridge loop.
- `prime_rpc.format_rpc_command()` emits `{"content": ...}`. Prime Agent v0.8.0 and v0.8.1
  require a `type` field and a `message` field.
- The Modal image does not install Node.js. Prime Agent v0.8.1 requires Node.js 22.8 or newer.
- `ModalRunner` does not pass the investigation ID or task brief into the sandbox.
- The World Gateway exists as a FastAPI app but no sandbox process starts it.
- Prime Agent does not emit `summary_submitted`. Its documented completion event is
  `agent_end`.
- The World Gateway is not registered as a Prime Agent tool surface.
- The Modal sandbox has no outbound network allowlist or block policy.
- The live investigation smoke script can time out without returning failure.

## Reliability and credential-boundary repair (2026-08-27)

Live investigation `e35a0370-69aa-404e-8024-200870d2d6c4` hung at 300 seconds.
Prime Agent v0.8.1 kept IPython active, called the control-plane SSE `/events`
endpoint from inside the sandbox, and hung because SSE keepalives prevented
`requests`' read timeout from firing. 5,441 events persisted with no
`summary_submitted` and no `investigation_finished`; Modal killed the container
(`exit=-2`) after a state-changing call. The World Gateway received only the
investigation ID, so state/evidence/diff returned unexplained empty defaults.
Prime inherited `BRIDGE_TOKEN`, `CONTROL_PLANE_CALLBACK_URL`, and provider
credentials.

The repair keeps Prime Agent v0.8.1's Python-native architecture: IPython and
the nine World Gateway extension tools remain enabled (no `--no-builtin-tools`).
It adds:

1. Separate control-plane authority (`BRIDGE_TOKEN`) from World Gateway
   authority (a per-run loopback `WORLD_GATEWAY_TOKEN` generated in the bridge).
   Prime Agent never receives `BRIDGE_TOKEN`.
2. A deliberately sanitized explicit child environment for Prime Agent
   (runtime basics, model-provider credentials, gateway wiring only) excluding
   `BRIDGE_TOKEN`, `CONTROL_PLANE_CALLBACK_URL`, task/control-plane data,
   database credentials, and unrelated secrets. The provider credential remains
   visible to Prime/IPython; a provider proxy is future hardening.
3. Truthful World Gateway context (investigation ID, world/slice metadata,
   fixture bundle ref, provenance, trace reference) and explicit
   `available: false` reasons for state/evidence/diff data that is not
   compiled. No invented fixture data.
4. A bounded watchdog for Prime tool executions that never end, using the
   official `{"type":"abort"}` RPC command. Tool executions are tracked by
   `toolCallId`; the watchdog fires on whichever comes first: the per-tool age
   (`TOOL_TIMEOUT_S`) or the overall run cutoff the bridge derives from the
   injected sandbox lifetime (`SANDBOX_TIMEOUT_S` minus recovery minus reserve),
   so a tool that starts late in the run is still aborted before Modal kills
   the container. On abort the bridge persists a warning, invalidates any stale
   `agent_end`, and makes one bounded recovery attempt instructing Prime to
   submit its summary without retrying. If no valid summary plus the
   corresponding fresh `agent_end` arrives within the recovery timeout (bounded
   to `SANDBOX_TIMEOUT_S` minus reserve), the run fails deterministically. The
   abort's own `agent_end` only closes the aborted run and never emits
   `investigation_finished`; the recovery `agent_start` opens the fresh run
   scope whose own `agent_end` counts. The watchdog budget is enforced on
   every `SandboxSpec` against the sandbox outer timeout and flows
   `Settings -> SandboxSpec -> Modal container env -> bridge`.

Completion after recovery therefore requires a valid summary plus the
corresponding fresh `agent_end`, never a stale `agent_end` from the aborted run
(the watchdog success test models the real no-summary live failure and recovery
sequence; an exact-order regression proves an abort's `agent_end` plus a
recovery summary cannot complete a run without the recovery run's own end).
Exactly-once summary persistence and the existing mutation/retry safety are
preserved. Deterministic bridge-seam regressions prove the watchdog aborts a
never-ending tool below the outer timeout
(`test_watchdog_aborts_hung_tool_and_recovers`,
`test_watchdog_recovery_expired_fails_deterministically`,
`test_watchdog_abort_agent_end_cannot_complete_recovery`,
`test_watchdog_fresh_recovery_agent_end_completes`), that the overall run
cutoff aborts a late-starting tool at the measured live shape (300s outer, hung
tool at ~140s) instead of relying on per-tool age
(`test_watchdog_run_budget_aborts_late_starting_tool`,
`test_watchdog_run_budget_fires_without_active_tool`), and contract-level
regressions enforce the watchdog budget and injected outer timeout against each
`SandboxSpec` (`test_sandbox_spec_rejects_watchdog_budget_exceeding_outer_timeout`,
`test_sandbox_spec_300s_smoke_configuration_fits_watchdog_budget`,
`test_modal_start_rejects_watchdog_budget_exceeding_sandbox_timeout`). The
repair was re-verified live on 2026-08-27 by investigation
`5d1c240d-a4ca-4faa-b213-cef4a696113f`: the sandbox exited 0 and PostgreSQL
stored exactly one summary and one `investigation_finished` event. This proves
the pipeline and cleanup behavior; it does not provide real business-world
evidence because compiled trace, state, and evidence data remain Phase 8.0 work.

## Locked upstream contract

Use Prime Agent v0.8.1. Do not target an unpinned stable channel.

- Install Node.js 22.8 or newer and npm before running the official Prime Agent installer.
- Launch RPC mode with `prime-agent --mode rpc --no-session`.
- Send line-delimited commands:

  ```json
  {"type":"prompt","message":"..."}
  {"type":"steer","message":"..."}
  {"type":"follow_up","message":"..."}
  ```

- Treat `agent_start` as agent readiness and `agent_end` as Prime Agent run completion.
- Map `tool_execution_start`, `tool_execution_update`, and `tool_execution_end` into the
  existing runner event contract without exposing raw credentials or unrestricted output.
- Register the World Gateway tools through a Prime Agent extension. The extension calls the
  gateway over loopback HTTP. Do not invent an unsupported RPC tool-registration message.

Official sources:

- `https://github.com/PrimeIntellect-ai/prime-agent/blob/v0.8.1/packages/coding-agent/docs/rpc.md`
- `https://github.com/PrimeIntellect-ai/prime-agent/blob/v0.8.1/packages/coding-agent/docs/extensions.md`
- `https://github.com/PrimeIntellect-ai/prime-agent/blob/v0.8.1/packages/coding-agent/src/modes/rpc/rpc-types.ts`
- `https://app.primeintellect.ai/prime-agent/install.sh`

If the live artifact differs from these pinned sources, stop and send a `[BLOCKED]` mesh
message. Do not adapt the protocol by guesswork.

## Scope boundaries

Do not add the Phase 8 experiment engine, world compiler, alert intake, web UI, BYOVM,
multi-user authentication, or automatic remediation.

Do not modify `graphify-out/`, the untracked interactive walkthrough skill, or generated HTML
files. Do not perform unrelated cleanup in the roadmap or reference workflow documentation.

Preserve existing API paths, database tables, CLI commands, and the provider-neutral
`CloudRunner` boundary unless this plan names a required contract change.

## Milestone 1: Correct the sandbox and RPC contracts

### Runner identity and inputs

1. Add `investigation_id: UUID` to `SandboxSpec`.
2. Keep `task_brief` as a first-class `SandboxSpec` field.
3. Add an explicit outbound domain allowlist to the sandbox contract. Use one name across the
   schema, settings, adapter, tests, and docs.
4. Reserve these sandbox variables so caller-supplied `request.env` cannot override them:
   `INVESTIGATION_ID`, `TASK_BRIEF`, `CONTROL_PLANE_CALLBACK_URL`, `BRIDGE_TOKEN`,
   `WORLD_GATEWAY_URL`, and the world/slice identity variables.
5. Put `BRIDGE_TOKEN` in `SandboxSpec.secrets`, not `SandboxSpec.env`. Reject a caller secret
   with the same reserved name.

### Prime Agent RPC adapter

1. Replace the content-only wire format with typed `prompt`, `steer`, and `follow_up`
   commands using the official `message` field.
2. Parse official RPC responses separately from events.
3. Map these official events into the existing domain events:
   - `agent_start` -> `agent_ready`
   - message text or reasoning deltas -> `message` or `thought`
   - `tool_execution_start` -> `tool_call`
   - `tool_execution_update` and `tool_execution_end` -> `tool_result`
   - terminal RPC errors -> `error`
   - `agent_end` -> `investigation_finished`
4. Preserve the original official event name in the payload for diagnosis.
5. Never map `agent_end` directly to `summary_submitted`. A valid run still requires the
   World Gateway summary tool.

### Modal image

1. Install a pinned Node.js 22 release and npm before Prime Agent.
2. Pin Prime Agent to v0.8.1 through the official installer-supported version control.
3. Fail the image build if `node --version`, `npm --version`, or `prime-agent --version` fails.
4. Copy the World Gateway extension into Prime Agent's project or user extension directory.
5. Keep Modal SDK calls inside `_modal_client.py`.

### Tests and review gate

Add focused tests that prove:

- every RPC command matches the official JSON shape;
- official response lines do not become false domain events;
- the named official event mappings are stable;
- the Modal adapter receives the investigation ID, task brief, token as a secret, extension,
  resource limits, and outbound policy;
- reserved environment and secret keys cannot be overridden;
- secret values stay absent from reprs, dumps, logs, and assertion output.

Run:

```bash
uv run ruff check .
uv run mypy src
uv run pytest tests/unit/runner tests/unit/agent_runner -q
```

Send `[DONE] M1` with changed files and command results. Stop for big dipper review.

## Milestone 2: Run Prime Agent and the World Gateway

### World Gateway extension

1. Add one small Prime Agent TypeScript extension that registers the nine approved
   investigation tools.
2. Keep the canonical tool names and HTTP routes in one mapping. If Prime Agent requires
   identifier-safe registration names, use underscore aliases only at the extension boundary
   and map them to the existing dotted HTTP routes.
3. Send tool arguments to `WORLD_GATEWAY_URL/tools/{canonical_name}` with the bearer token.
4. Return typed tool results and useful errors. Do not return the token, environment, or raw
   HTTP headers to the model.
5. Do not add npm dependencies for the extension unless the built-in runtime cannot express
   the contract.

### Gateway lifecycle and event ownership

1. Start the gateway on loopback before Prime Agent starts.
2. Pass one event callback into the gateway so the bridge records authoritative `tool_call`
   and `tool_result` events from actual gateway requests.
3. When `summary.submit` succeeds, emit exactly one `summary_submitted` event containing
   `findings`, `next_step`, and `evidence_refs`.
4. Validate the summary payload before emitting the event. A malformed summary returns a
   visible tool error and cannot complete the investigation.
5. Unknown tools remain 404, and a wrong token remains rejected.

### Bridge lifecycle

Implement `SandboxBridge.run()` as the owner of these resources:

1. Start the loopback World Gateway.
2. Start `prime-agent --mode rpc --no-session` with stdin, stdout, and stderr pipes.
3. Emit and flush `investigation_started`.
4. Send the task brief as the first `prompt` command.
5. Read stdout until completion and map official events.
6. Poll the control-plane inbox and write `prompt`, `steer`, or `follow_up` RPC commands.
7. Emit heartbeats every configured interval.
8. Flush at 50 events or 500 milliseconds, whichever occurs first.
9. Stop successfully only after a valid `summary.submit` and `agent_end` have both occurred.
10. On EOF, nonzero process exit, malformed protocol output, or missing summary, emit a
    visible `error` event and return nonzero.
11. Retry only failures that occur before any gateway mutation. Do not replay a task after a
    state-changing tool call.
12. On every exit path, close stdin, terminate remaining child processes, stop the gateway,
    flush buffered events, and close the HTTP client.

Do not hide stderr. Capture a bounded, redacted diagnostic tail for the final error event.

### Tests and review gate

Use an injected fake RPC process, a loopback gateway, and a fake control plane. Prove:

- the task brief is the first prompt;
- inactive steering downgrades to `follow_up`, while active steering uses `steer`;
- prompt, steer, and follow-up commands reach stdin in order;
- gateway calls produce ordered tool events;
- a valid summary plus `agent_end` completes once;
- `agent_end` without a summary fails visibly;
- summary without `agent_end` does not report clean process completion;
- heartbeats and timed flushes occur below the batch limit;
- duplicate retries do not duplicate persisted events;
- child-process, gateway-start, protocol, and control-plane failures clean up resources;
- secrets and stderr redaction hold on failures.

Run:

```bash
uv run ruff check .
uv run mypy src
uv run pytest tests/unit/agent_runner tests/unit/investigation -q
```

Send `[DONE] M2` with changed files and command results. Stop for big dipper review.

## Milestone 3: Wire the control plane, Modal policy, and smoke evidence

### Control-plane wiring

1. Build `SandboxSpec` with the real investigation ID and task brief.
2. Place the bridge token in the Modal secret set.
3. Require a reachable `CONTROL_PLANE_PUBLIC_URL` when Modal is enabled. Do not silently use
   `127.0.0.1` for a cloud sandbox.
4. Validate the outbound allowlist before provisioning. It must include the control-plane host
   and the configured model-provider host. Reject missing or malformed entries.
5. Keep the persisted token hash as the only database representation of the bridge token.
6. Preserve event idempotency and terminal-state rules.

### Modal network policy

1. Pass the explicit outbound domain allowlist to Modal's native sandbox network controls.
2. Do not use unrestricted egress when Modal is enabled.
3. Keep the World Gateway bound to loopback.
4. Add `MODAL_TIMEOUT_S` and the outbound allowlist setting to `.env.example` with safe
   defaults and concise comments.

### End-to-end and smoke tests

1. Replace the current offline test's hand-posted bridge events with a real
   `SandboxBridge.run()` driven by a fake Prime RPC process. Run through the real FastAPI app,
   inbox, gateway, persistence, SSE replay, summary, and fake sandbox termination.
2. Keep a focused PostgreSQL reconnect test for ordered, duplicate-free replay.
3. Make `scripts/investigate_smoke.py` return nonzero when it times out, when the sandbox fails,
   when no summary persists, or when the sandbox is not terminated.
4. Make the script print the investigation ID and a concise result without printing tokens or
   credentials.
5. Keep `scripts/modal_smoke.py` as the smaller image/build/PATH proof.

Run offline gates first:

```bash
uv run ruff check .
uv run mypy src
uv run pytest tests/unit -q
uv run pytest tests/integration/investigation -q
uv run pytest tests/integration/runner/test_modal_live.py -q
```

The Modal live test must skip cleanly without credentials. Send `[DONE] M3-OFFLINE` and stop
for big dipper review.

After offline review, a human with Modal and model credentials runs:

```bash
uv run python scripts/modal_smoke.py
SIMULATE_LIVE_E2E=1 MODAL_ENABLED=true uv run python scripts/investigate_smoke.py
```

Do not mark the live MVP complete if either command skips or fails.

## Milestone 4: Align documentation and Linear

Start this milestone only after the live evidence passes.

### Documentation changes

1. `README.md`: describe the verified cloud investigation workflow and link to the smoke-test
   instructions.
2. `ARCHITECTURE.md`: describe implemented behavior separately from later Phase 8 target
   architecture. Replace WebSocket claims with SSE. Keep the full Phase 8 compiler and
   experiment engine marked in progress.
3. `BUILD_ROADMAP.md`: keep only focused Phase 8 status and evidence changes. Restore unrelated
   requirements removed by the current large uncommitted rewrite. Record exact test results,
   live commands, date, and commit only after those values exist.
4. `PHASE8_MVP_PLAN.md`: add an archived/completed banner and mark only acceptance items backed
   by evidence. Keep it as the historical implementation plan.
5. `scripts/README.md`: state what each smoke script proves, required configuration, success,
   skip, and failure behavior.
6. `codemap.md` and package codemaps: describe the actual bridge loop, gateway extension,
   event flow, and security boundary. Remove links to generated HTML unless those artifacts are
   approved in a separate change.
7. `.env.example`: document every Phase 8 runtime setting without adding secrets.

Do not include `docs/interactive_codemap.html`, `docs/dataflow.html`, `codemap.html`, or
`.agents/skills/interactive-walkthrough/` in this change.

### Linear changes

1. Add a correction and final evidence comment to THE-18. Explain that the first completion
   comment covered the offline control plane, then record the repaired live path and evidence.
2. Update acceptance checkboxes only after each condition is true.
3. Do not rename the parent to imply that the full Phase 8 compiler and experiment engine are
   complete.

### Final verification

Run:

```bash
uv run ruff check .
uv run mypy src
uv run pytest tests/unit -q
uv run pytest
```

If the full integration suite still has failures that predate this branch, show that the same
tests fail at the branch base and report them separately. Do not call a failing suite green.

Send `[DONE] M4` with:

- changed files;
- exact check results;
- exact live smoke results;
- remaining risks;
- a concise diff summary for user review.

Stop. Big dipper reviews the complete diff. The user reviews after big dipper. Only big dipper
pushes, and only after the user approves.

## Acceptance

This plan is complete only when all of these are true:

- The image builds with Node.js 22.8 or newer and Prime Agent v0.8.1 on PATH.
- A real RPC process accepts the task brief and all three chat modes.
- The nine World Gateway tools are callable through the registered extension.
- The bridge emits ordered, idempotent events and periodic heartbeats.
- A valid `summary.submit` persists one summary.
- `agent_end` and summary submission produce clean completion and sandbox termination.
- Failures produce a visible operational error and clean up resources.
- Modal enforces the configured outbound domain allowlist.
- Disconnect and reconnect replay all persisted events without duplicates.
- Both live smoke commands pass rather than skip.
- Documentation says Phase 8 MVP is complete and full Phase 8 remains in progress.
- Linear contains the final evidence record.
- Ruff, mypy, unit tests, relevant integration tests, and the full test suite have honest
  reported results.
