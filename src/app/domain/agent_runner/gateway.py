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

Requires Bearer token authentication matching the sandbox BRIDGE_TOKEN.
"""

from typing import Any

from fastapi import FastAPI, Header, HTTPException, Request, status
from pydantic import BaseModel, Field

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


class ToolCallRequest(BaseModel):
    """Payload for invoking a tool via World Gateway."""

    arguments: dict[str, Any] = Field(default_factory=dict)


class ToolCallResponse(BaseModel):
    """Response returned by World Gateway tool invocation."""

    tool: str
    result: dict[str, Any] = Field(default_factory=dict)


class WorldGatewayService:
    """In-memory or database-backed mock service provider for sandbox tools."""

    def __init__(self, bridge_token: str, context: dict[str, Any] | None = None) -> None:
        self.bridge_token = bridge_token
        self.context = context or {}
        self.submitted_summary: dict[str, Any] | None = None

    def handle_tool(self, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Dispatch tool invocation to respective skill handler."""
        if tool_name == "trace.read":
            return {"trace": self.context.get("trace", {"id": "trace_default"})}
        if tool_name == "world.describe":
            return {
                "world_id": self.context.get("world_id", "world_default"),
                "slice_name": self.context.get("slice_name", "slice_default"),
                "description": "Disposable sandbox reproduction environment",
            }
        if tool_name == "environment.status":
            return {"status": "healthy", "database": "connected"}
        if tool_name == "scenario.run":
            return {"status": "executed", "scenario": arguments.get("scenario", "default")}
        if tool_name == "state.inspect":
            return {"state": self.context.get("state", {"tables": {}})}
        if tool_name == "state.diff":
            return {"diff": self.context.get("diff", {})}
        if tool_name == "evidence.read":
            return {"evidence": self.context.get("evidence", {})}
        if tool_name == "proposal.create":
            return {"proposal_id": "prop_123", "status": "created"}
        if tool_name == "summary.submit":
            self.submitted_summary = arguments
            return {"status": "submitted", "summary": arguments}

        return {}


def create_gateway_app(bridge_token: str, context: dict[str, Any] | None = None) -> FastAPI:
    """Create and configure the FastAPI World Gateway application."""
    app = FastAPI(title="World Gateway", docs_url=None, redoc_url=None, openapi_url=None)
    service = WorldGatewayService(bridge_token=bridge_token, context=context)

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
        if token != service.bridge_token:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Invalid bridge token",
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
