# src/app/domain/agent_runner/

## Responsibility

Runs inside the investigation sandbox. It supervises Prime Agent 0.8.1, exposes a protected
loopback World Gateway, converts Prime RPC output into domain events, relays control-plane chat,
applies the bounded tool watchdog, and flushes ordered events to the control plane.

## Design

- **Bridge / process supervisor:** `SandboxBridge.run()` owns gateway startup, Prime creation, the
  concurrent stdout, stderr, inbox, heartbeat, flush, and watchdog loops, and cleanup.
- **Separated authority:** `BRIDGE_TOKEN` authenticates bridge-to-control-plane HTTP. A random
  `WORLD_GATEWAY_TOKEN` authenticates Prime-to-gateway calls. Prime receives only the latter.
- **Sanitized child environment:** Prime receives runtime basics, approved model credentials, and
  loopback wiring. It does not inherit task, callback, database, or control-plane secrets.
- **RPC adapter:** `prime_rpc.py` serializes official `prompt`, `steer`, `follow_up`, and `abort`
  commands without local request IDs, filters command responses, and maps Prime events to domain
  events.
- **Micro-gateway / extension adapter:** `gateway.py` exposes nine dotted routes.
  `world_gateway.ts` registers underscore aliases and forwards calls with a 30-second timeout.
- **Truthful MVP boundary:** trace telemetry, state, diffs, and evidence return unavailable results
  when not compiled. `scenario.run` and `proposal.create` return fixed MVP-shaped results.
- **Mutation-aware retry:** the bridge marks mutation from authoritative gateway tool-call events;
  crashes retry only before `scenario.run`, `proposal.create`, or `summary.submit`.
- **Two-deadline watchdog:** the bridge tracks tool calls by `toolCallId` and aborts once at the
  per-tool timeout or overall run cutoff, then permits one bounded recovery steer.
- **Two-part completion:** success requires a valid summary, a fresh matching `agent_end`, and
  successful event persistence.

## Key Files

- `bridge.py`: lifecycle, control-plane client, retries, watchdog, child environment, entrypoint.
- `gateway.py`: loopback app, nine handlers, summary validation, authoritative tool events.
- `prime_rpc.py`: Prime RPC serialization and event conversion.
- `extensions/world_gateway.ts`: Prime extension installed in the Modal image.
- `__init__.py`: public exports.

## Data and Control Flow

### Startup and Chat

1. `ModalRunner` starts `python -m app.domain.agent_runner.bridge` with investigation metadata.
2. `main()` validates UUID and timeout variables, builds world context, and starts the bridge.
3. The bridge starts the gateway on `127.0.0.1:8001`, flushes `investigation_started`, launches
   `prime-agent --mode rpc --no-session`, and sends the task brief as the first prompt.
4. It polls `GET /internal/inbox` by message-ID cursor. Inactive `steer` becomes `follow_up`;
   a prompt during an active run uses Prime's `streamingBehavior="steer"`.

### Tools and Events

1. Prime calls an underscore extension tool. The extension maps it to one of nine dotted gateway
   routes and posts typed arguments with the gateway Bearer token.
2. The gateway emits authoritative `tool_call` and `tool_result` callbacks. A valid
   `summary.submit` emits `summary_submitted` once.
3. Prime stdout maps `agent_start -> agent_ready`, text/thinking deltas to messages or thoughts,
   tool start/update/end to tool events, errors to `error`, and `agent_end` to
   `investigation_finished`. The original event name remains in each payload.
4. The bridge assigns sequence numbers and posts batches of at most 50 or every 500 ms to
   `POST /internal/events`. One lock serializes buffer snapshot, POST, and eviction.

### Watchdog and Completion

1. The watchdog fires at the first per-tool timeout or
   `sandbox timeout - recovery timeout - 60 seconds`.
2. It sends one abort, records a reasoned warning, ignores the aborted run's closing
   `agent_end`, and sends one recovery steer. A later `agent_start` opens the run whose end counts.
3. Summary plus fresh end sets completion. The bridge exits `0` only after a full forced flush.
4. Recovery expiry, protocol failure, post-mutation crash, repeated pre-mutation crashes, or final
   persistence failure emits a fatal error and exits nonzero.
5. Every path closes Prime input, bounds process shutdown, stops the gateway, flushes again, and
   closes the owned HTTP client. Stderr is capped at 50 redacted lines.

## Security Boundary

- The gateway binds only to container loopback.
- Prime cannot read the bridge token or control-plane callback through its child environment.
- Diagnostics redact both tokens.
- Provider credentials remain available to Prime and IPython; a provider proxy is future work.
- Modal applies the configured outbound-domain allowlist to the sandbox.

## Integration

- **Launched by:** `app.domain.runner.modal_runner.ModalRunner`.
- **HTTP peer:** `app.api.investigations_router` internal endpoints.
- **Lifecycle consumer:** `app.domain.investigation.service.InvestigationService`.
- **Shared contracts:** `app.domain.runner.schemas`.
- **External runtimes:** Prime Agent, FastAPI/Uvicorn, `httpx`, Node.js fetch, and TypeBox.
