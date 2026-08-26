"""LangGraph compilation for the controlled support workflow."""

from __future__ import annotations

from typing import Any, cast

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph

from app.domain.agent.schemas import RouteIntent
from app.domain.workflow.models import EvidenceBundle, SupportState
from app.domain.workflow.nodes import (
    RETRY_POLICY,
    WorkflowNodeDependencies,
    make_confirmation_node,
    make_escalation_node,
    make_execute_node,
    make_order_node,
    make_policy_node,
    make_proposal_node,
    make_rejection_node,
    make_response_node,
    make_route_node,
)


def _after_route(state: SupportState) -> str:
    if state.get("escalation") is not None:
        return "escalate"
    route = state["route"].intent
    if route is RouteIntent.ORDER_STATUS:
        return "retrieve_order"
    if route is RouteIntent.POLICY:
        return "retrieve_policy"
    if route is RouteIntent.REFUND:
        return "retrieve_order"
    return "escalate"


def _after_evidence(state: SupportState) -> str:
    if state.get("escalation") is not None:
        return "escalate"
    if state["route"].intent is RouteIntent.REFUND:
        if (state.get("evidence") or EvidenceBundle()).policy is None:
            return "retrieve_policy"
        return "propose_refund"
    return "respond"


def _after_proposal(state: SupportState) -> str:
    return "escalate" if state.get("escalation") is not None else "confirmation"


def _after_confirmation(state: SupportState) -> str:
    confirmation = state.get("confirmation")
    if confirmation is None:
        return "escalate"
    return "execute_refund" if confirmation.decision.value == "confirm" else "reject"


def _after_execution(state: SupportState) -> str:
    return "escalate" if state.get("escalation") is not None else END


def _after_response(state: SupportState) -> str:
    return "escalate" if state.get("escalation") is not None else END


def compile_support_graph(
    dependencies: WorkflowNodeDependencies,
    checkpointer: BaseCheckpointSaver[Any],
) -> Any:
    """Compile one versioned graph with an explicit confirmation interrupt."""
    builder: Any = StateGraph(SupportState)
    builder.add_node("route", make_route_node(dependencies), retry_policy=RETRY_POLICY)
    builder.add_node(
        "retrieve_order",
        make_order_node(dependencies),
        retry_policy=RETRY_POLICY,
    )
    builder.add_node(
        "retrieve_policy",
        make_policy_node(dependencies),
        retry_policy=RETRY_POLICY,
    )
    builder.add_node("propose_refund", make_proposal_node(dependencies))
    builder.add_node("confirmation", make_confirmation_node(dependencies))
    builder.add_node("execute_refund", make_execute_node(dependencies))
    builder.add_node("reject", make_rejection_node(dependencies))
    builder.add_node("respond", make_response_node(dependencies), retry_policy=RETRY_POLICY)
    builder.add_node("escalate", make_escalation_node(dependencies))

    builder.add_edge(START, "route")
    builder.add_conditional_edges("route", _after_route)
    builder.add_conditional_edges("retrieve_order", _after_evidence)
    builder.add_conditional_edges("retrieve_policy", _after_evidence)
    builder.add_conditional_edges("propose_refund", _after_proposal)
    builder.add_conditional_edges("confirmation", _after_confirmation)
    builder.add_conditional_edges("execute_refund", _after_execution)
    builder.add_edge("reject", END)
    builder.add_conditional_edges("respond", _after_response)
    builder.add_edge("escalate", END)
    return cast(Any, builder.compile(checkpointer=checkpointer, name="support-workflow"))

