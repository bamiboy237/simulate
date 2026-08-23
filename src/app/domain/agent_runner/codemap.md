# src/app/domain/agent_runner/

## Responsibility
In-Sandbox Agent Harness & Communication Bridge. Operates inside the isolated sandbox container to manage agent lifecycle, adapt line-based RPC communication (Prime Agent protocol), expose local investigation tools via the World Gateway, and reliably relay events and messages between the investigating agent and the remote control plane.

## Design Patterns
- **Ambassador / Sidecar Bridge Pattern:** `SandboxBridge` runs alongside the agent inside the sandbox, managing inbound chat polling, outbound event batching, and periodic heartbeats.
- **Adapter / Serializer Pattern:** `prime_rpc.py` converts user chat messages into JSON line-delimited commands (`format_rpc_command`) and parses raw agent stdout streams into typed `RunnerEvent` domain events (`parse_rpc_output_line`).
- **Service Locator / Micro-Gateway:** `WorldGatewayService` and `create_gateway_app` expose a lightweight FastAPI HTTP service inside the container providing the 9 investigation skill tools (`trace.read`, `world.describe`, `environment.status`, `scenario.run`, `state.inspect`, `state.diff`, `evidence.read`, `proposal.create`, `summary.submit`).

## Key Files
- `bridge.py`: `SandboxBridge` async event loop, message polling, and batch dispatch.
- `gateway.py`: `WorldGatewayService` and FastAPI application exposing the 9 investigation tools.
- `prime_rpc.py`: Prime Agent RPC serialization and stdout line parser.

## Data & Control Flow
1. **Bridge Startup:** `main()` starts `SandboxBridge.run()`, emitting an `EventType.investigation_started` event flushed to `POST /internal/events` on the control plane.
2. **Inbound Communication:** `SandboxBridge.poll_inbox()` long-polls `GET /internal/inbox` with exponential backoff; incoming messages undergo mode translation (including the steer downgrade rule: downgrading `steer` to `follow_up` if the agent is not active), are serialized via `format_rpc_command()`, and dispatched to the agent process.
3. **Outbound Telemetry:** Agent stdout is read line-by-line via `handle_stdout_line()`, parsed via `parse_rpc_output_line()`, buffered in `event_buffer`, and batched (up to 50 events or 500ms intervals) to `POST /internal/events`.
4. **Tool Execution:** The agent issues local HTTP requests to `/tools/{tool_name}`; `WorldGatewayService` verifies the Bearer bridge token and serves sandbox state inspection, trace replay, or proposal creation.
5. **Completion:** When `summary.submit` generates `EventType.summary_submitted`, all pending events are flushed and the bridge finishes cleanly.

## Integration
- **Consumed by:** Modal sandbox entrypoint (`python -m app.domain.agent_runner.bridge`), Prime Agent CLI / tested AI agents.
- **Depends on:** `app.domain.runner.schemas` (RunnerEvent, EventType), `httpx` (HTTP streaming client), `fastapi` (World Gateway app).
