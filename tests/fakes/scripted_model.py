"""Scripted hosted-model boundary for offline runner tests.

The scripted model is a Pydantic AI ``FunctionModel`` that follows one
deterministic plan: a routing decision, an ordered list of tool calls, and a
final answer. The routing agent has no tools, so the scripted function
returns the routing decision there; the answer agents carry tools, so the
function emits the next planned tool call until every planned call returned,
then emits the final answer. After a retry prompt the function re-emits the
current tool call, which reproduces the reference retry behavior offline.
"""

import json
from dataclasses import dataclass
from uuid import UUID

from pydantic_ai import ModelResponse
from pydantic_ai.messages import TextPart, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.usage import RequestUsage


@dataclass(frozen=True)
class ScriptedToolCall:
    """This class stores one planned tool call."""

    tool: str
    arguments: dict[str, object]


@dataclass(frozen=True)
class ScriptedPlan:
    """This class stores one deterministic model behavior for a test run."""

    routing: dict[str, object]
    tool_calls: tuple[ScriptedToolCall, ...] = ()
    answer: dict[str, object] | None = None
    cost: float = 0.001


def _usage(cost: float) -> RequestUsage:
    return RequestUsage(input_tokens=12, output_tokens=6, cost=cost)


def build_scripted_model(plan: ScriptedPlan) -> FunctionModel:
    """This function builds a scripted model that follows one plan.

    The first invocation is always the routing agent, which has no tools.
    Later invocations belong to the answer agent: the function emits the
    next planned tool call until every planned call returned, then emits the
    final answer. After a retry prompt the function re-emits the current
    tool call because no planned call returned.
    """
    calls: list[int] = [0]

    def scripted(messages: list[object], info: AgentInfo) -> ModelResponse:
        calls[0] += 1
        if not info.function_tools and calls[0] == 1:
            return ModelResponse(
                parts=[TextPart(json.dumps(plan.routing))],
                usage=_usage(plan.cost),
            )
        returns = 0
        for message in messages:
            for part in getattr(message, "parts", ()):
                if isinstance(part, ToolReturnPart):
                    returns += 1
        if plan.answer is not None and returns >= len(plan.tool_calls):
            return ModelResponse(
                parts=[TextPart(json.dumps(plan.answer))],
                usage=_usage(plan.cost),
            )
        call = plan.tool_calls[returns]
        return ModelResponse(
            parts=[ToolCallPart(tool_name=call.tool, args=dict(call.arguments))],
            usage=_usage(plan.cost),
        )

    return FunctionModel(scripted, model_name="scripted-support-model")


def install_scripted_model(monkeypatch: object, plan: ScriptedPlan) -> FunctionModel:
    """This function installs one scripted model at the hosted-model boundary."""
    model = build_scripted_model(plan)

    def build(config: object) -> FunctionModel:
        return model

    monkeypatch.setattr(  # type: ignore[attr-defined]
        "app.adapters.pydantic_ai_agent.build_pydantic_ai_model",
        build,
    )
    return model


def order_status_plan(order_id: UUID, message: str = "Your order is shipped.") -> ScriptedPlan:
    """This function plans a grounded order-status turn."""
    return ScriptedPlan(
        routing={"intent": "order_status", "confidence": 0.95, "order_id": str(order_id)},
        tool_calls=(ScriptedToolCall("_get_order_status", {"order_id": str(order_id)}),),
        answer={"intent": "order_status", "message": message},
    )


def policy_plan(policy_slug: str = "refund-and-delivery") -> ScriptedPlan:
    """This function plans a grounded policy turn."""
    return ScriptedPlan(
        routing={"intent": "policy", "confidence": 0.9, "policy_slug": policy_slug},
        tool_calls=(ScriptedToolCall("_get_policy", {"slug": policy_slug}),),
        answer={
            "intent": "policy",
            "message": "The policy says delivered orders may be returned.",
        },
    )


def refund_plan(
    order_id: UUID,
    *,
    confirmed: bool,
) -> ScriptedPlan:
    """This function plans a refund turn that attempts execution."""
    calls = [
        ScriptedToolCall("_get_order_status", {"order_id": str(order_id)}),
        ScriptedToolCall(
            "_propose_refund",
            {"order_id": str(order_id), "reason": "Customer requested a refund"},
        ),
    ]
    if confirmed:
        calls.append(ScriptedToolCall("_confirm_refund", {"order_id": str(order_id)}))
    return ScriptedPlan(
        routing={"intent": "refund", "confidence": 0.9, "order_id": str(order_id)},
        tool_calls=tuple(calls),
        answer={"intent": "refund", "message": "Your refund is being processed."},
    )


def escalate_plan() -> ScriptedPlan:
    """This function plans an escalation turn with no tool calls."""
    return ScriptedPlan(
        routing={"intent": "escalate", "confidence": 0.8},
        tool_calls=(),
        answer={"intent": "escalate", "message": "A human agent will follow up."},
    )
