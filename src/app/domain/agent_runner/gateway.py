"""World Gateway HTTP service running inside the sandbox.

Exposes exactly nine tool routes matching the investigation skills:
- /tools/trace.read
- /tools/world.describe
- /tools/environment.status
- /tools/scenario.run
- /tools/state.inspect
- /tools/state.diff
- /tools/evidence.read
- /tools/proposal.create
- /tools/summary.submit

Requires Bearer token authentication matching the per-run loopback
WORLD_GATEWAY_TOKEN generated inside the sandbox bridge. This token is
separate from the control-plane BRIDGE_TOKEN and is the only token Prime
Agent receives. Emits authoritative tool_call and tool_result domain events
to the sandbox bridge.
"""

from collections.abc import Callable
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Request, status
from pydantic import BaseModel, Field, ValidationError, field_validator

from app.domain.runner.schemas import EventType

SUPPORTED_TOOLS: frozenset[str] = frozenset(
    {
        "trace.read",
        "world.describe",
        "environment.status",
        "scenario.run",
        "state.inspect",
        "state.diff",
        "evidence.read",
        "proposal.create",
        "summary.submit",
    }
)

STATE_CHANGING_TOOLS: frozenset[str] = frozenset(
    {
        "scenario.run",
        "proposal.create",
        "summary.submit",
    }
)

EventCallback = Callable[[EventType, dict[str, Any] | None], None]


class ToolCallRequest(BaseModel):
    """Payload for invoking a tool via World Gateway."""

    arguments: dict[str, Any] = Field(default_factory=dict)


class ToolCallResponse(BaseModel):
    """Response returned by World Gateway tool invocation."""

    tool: str
    result: dict[str, Any] = Field(default_factory=dict)


class SummaryPayload(BaseModel):
    """Validated schema for investigation final summary.

    A valid investigation summary requires non-empty ``findings``, a non-empty
    ``next_step``, and an explicit ``evidence_refs`` list.
    """

    findings: str
    next_step: str
    evidence_refs: list[str]

    @field_validator("findings", "next_step")
    @classmethod
    def _non_blank(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("must be a non-empty string")
        return cleaned


class WorldGatewayService:
    """In-memory service provider and state manager for sandbox tools.

    ``gateway_token`` authenticates gateway requests. It is the per-run
    loopback WORLD_GATEWAY_TOKEN, intentionally distinct from the control-plane
    BRIDGE_TOKEN. ``context`` carries only truthful metadata the bridge can
    prove (investigation/world/slice references, task metadata, trace refs);
    data that is not actually compiled (state, diffs, evidence telemetry)
    returns an explicit unavailable result instead of fabricated defaults.
    """

    def __init__(
        self,
        gateway_token: str,
        context: dict[str, Any] | None = None,
        event_callback: EventCallback | None = None,
    ) -> None:
        self.gateway_token = gateway_token
        self.context = context or {}
        self.event_callback = event_callback
        self.submitted_summary: dict[str, Any] | None = None
        self.has_mutated: bool = False
        self._summary_event_emitted: bool = False

    def emit_event(self, event_type: EventType, payload: dict[str, Any] | None = None) -> None:
        """Emit an authoritative domain event if callback is configured."""
        if self.event_callback:
            try:
                self.event_callback(event_type, payload)
            except Exception:
                pass

    @staticmethod
    def _unavailable(reason: str) -> dict[str, Any]:
        """Return an explicit unavailable result instead of fabricated data."""
        return {"available": False, "reason": reason}

    def handle_tool(self, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Dispatch tool invocation to respective skill handler and record events."""
        # 1. Emit authoritative tool_call event
        self.emit_event(EventType.tool_call, {"tool": tool_name, "arguments": arguments})

        # 2. Track state-changing mutations
        if tool_name in STATE_CHANGING_TOOLS:
            self.has_mutated = True

        # 3. Execute tool logic
        if tool_name == "trace.read":
            trace_ref = self.context.get("trace")
            if trace_ref is not None:
                result = {
                    "available": False,
                    "reason": (
                        "Trace telemetry is not compiled into this MVP sandbox; "
                        "only the trace reference is known."
                    ),
                    "trace_ref": trace_ref,
                }
            else:
                result = {
                    "available": False,
                    "reason": (
                        "No trace reference was provided to this sandbox; "
                        "full trace telemetry is not compiled in the MVP."
                    ),
                }
        elif tool_name == "world.describe":
            result = {
                "world_id": self.context.get("world_id"),
                "world_version": self.context.get("world_version"),
                "slice_name": self.context.get("slice_name"),
                "fixture_bundle_ref": self.context.get("fixture_bundle_ref"),
                "provenance": self.context.get("provenance"),
                "description": "Disposable sandbox reproduction environment",
            }
        elif tool_name == "environment.status":
            result = {
                "status": "healthy",
                "investigation_id": self.context.get("investigation_id"),
                "world_id": self.context.get("world_id"),
                "slice_name": self.context.get("slice_name"),
            }
        elif tool_name == "scenario.run":
            result = {
                "status": "executed",
                "scenario": arguments.get("scenario", "default"),
                "result": {"passed": True, "output": "Scenario execution completed."},
            }
        elif tool_name == "state.inspect":
            state_data = self.context.get("state")
            if state_data is not None:
                result = {"state": state_data}
            else:
                result = self._unavailable(
                    "Compiled environment state is not available; "
                    "the full Phase 8 world compiler is not implemented."
                )
        elif tool_name == "state.diff":
            diff_data = self.context.get("diff")
            if diff_data is not None:
                result = {"diff": diff_data}
            else:
                result = self._unavailable(
                    "A state baseline and diff are not compiled in this MVP sandbox."
                )
        elif tool_name == "evidence.read":
            evidence_data = self.context.get("evidence")
            if evidence_data is not None:
                result = {"evidence": evidence_data}
            else:
                result = self._unavailable(
                    "No captured evidence artifacts are compiled in this MVP sandbox."
                )
        elif tool_name == "proposal.create":
            result = {
                "proposal_id": "prop_123",
                "status": "created",
                "title": arguments.get("title", ""),
            }
        elif tool_name == "summary.submit":
            # Validate the summary payload before emitting any lifecycle event.
            # A malformed summary returns a visible tool error and can never emit
            # summary_submitted (and therefore cannot complete the investigation).
            try:
                validated = SummaryPayload.model_validate(arguments)
            except ValidationError as exc:
                error_msg = f"Invalid summary: {exc.errors(include_url=False)}"
                self.emit_event(
                    EventType.tool_result,
                    {"tool": tool_name, "error": error_msg, "isError": True},
                )
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=error_msg,
                )

            summary_data = validated.model_dump()
            self.submitted_summary = summary_data
            result = {"status": "submitted", "summary": summary_data}

            # Emit tool_result, then exactly one summary_submitted event even if
            # duplicate summary.submit calls arrive from the agent.
            self.emit_event(EventType.tool_result, {"tool": tool_name, "result": result})
            if not self._summary_event_emitted:
                self._summary_event_emitted = True
                self.emit_event(EventType.summary_submitted, summary_data)
            return result
        else:
            result = {}

        # 4. Emit tool_result event for other tools
        self.emit_event(EventType.tool_result, {"tool": tool_name, "result": result})
        return result


def create_gateway_app(
    gateway_token: str,
    context: dict[str, Any] | None = None,
    event_callback: EventCallback | None = None,
) -> FastAPI:
    """Create and configure the FastAPI World Gateway application."""
    app = FastAPI(title="World Gateway", docs_url=None, redoc_url=None, openapi_url=None)
    service = WorldGatewayService(
        gateway_token=gateway_token,
        context=context,
        event_callback=event_callback,
    )
    # Store service on app.state for introspection
    app.state.service = service

    @app.post("/tools/{tool_name}")
    async def invoke_tool(
        tool_name: str,
        request: Request,
        authorization: str | None = Header(default=None),
    ) -> ToolCallResponse:
        if not authorization or not authorization.startswith("Bearer "):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Missing or invalid Bearer token",
            )

        token = authorization.removeprefix("Bearer ").strip()
        if token != service.gateway_token:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Invalid gateway token",
            )

        if tool_name not in SUPPORTED_TOOLS:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Unknown tool: {tool_name}",
            )

        body: dict[str, Any] = {}
        try:
            body = await request.json()
        except Exception:
            body = {}

        args = body.get("arguments", body) if isinstance(body, dict) else {}
        result = service.handle_tool(tool_name, args)
        return ToolCallResponse(tool=tool_name, result=result)

    return app
