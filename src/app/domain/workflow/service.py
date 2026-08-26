"""Service boundary for starting and safely resuming support workflows."""

from __future__ import annotations

import asyncio
import re
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import UUID, uuid4

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from app.domain.agent.instructions import POLICY_SLUG
from app.domain.agent.schemas import RouteIntent, RoutingDecision
from app.domain.support.errors import Forbidden, InvalidTransition
from app.domain.support.repository import SupportRepository
from app.domain.support.schemas import (
    CreateTicketCommand,
    OrderRead,
    PolicyDocumentRead,
    TicketRead,
)
from app.domain.support.service import REFUNDABLE_ORDER_STATUSES, SupportService
from app.domain.workflow.errors import (
    InvalidWorkflowResume,
    WorkflowActorMismatch,
    WorkflowExpired,
    WorkflowNotFound,
)
from app.domain.workflow.graph import compile_support_graph
from app.domain.workflow.models import (
    WORKFLOW_ID,
    WORKFLOW_VERSION,
    EvidenceBundle,
    ProposedAction,
    SupportState,
    WorkflowRequest,
    WorkflowResponse,
)
from app.domain.workflow.nodes import WorkflowNodeDependencies
from app.telemetry.recorder import TraceRecorder

_UUID_PATTERN = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[1-5][0-9a-fA-F]{3}-[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}"
)


class RuleRouter:
    """A deterministic offline router used by the workflow service default."""

    async def route(self, message: str) -> RoutingDecision:
        lowered = message.lower()
        order_match = _UUID_PATTERN.search(message)
        order_id = UUID(order_match.group(0)) if order_match else None
        if any(word in lowered for word in ("refund", "money back", "cancel")):
            return RoutingDecision(
                intent=RouteIntent.REFUND,
                confidence=0.99,
                order_id=order_id,
                policy_slug=POLICY_SLUG,
            )
        if any(word in lowered for word in ("policy", "return", "eligible", "days")):
            return RoutingDecision(
                intent=RouteIntent.POLICY,
                confidence=0.95,
                policy_slug=POLICY_SLUG,
            )
        if any(word in lowered for word in ("status", "where", "track", "shipped")):
            return RoutingDecision(
                intent=RouteIntent.ORDER_STATUS,
                confidence=0.95,
                order_id=order_id,
            )
        return RoutingDecision(intent=RouteIntent.ESCALATE, confidence=0.5)


class SupportServiceOrderRetriever:
    def __init__(self, support: SupportService) -> None:
        self._support = support

    async def get_order(self, order_id: UUID, actor_id: UUID) -> OrderRead:
        return await self._support.get_order(order_id, actor_id)


class SupportServicePolicyRetriever:
    def __init__(self, support: SupportService) -> None:
        self._support = support

    async def get_policy(self, slug: str) -> PolicyDocumentRead:
        return await self._support.get_latest_policy(slug)


class SupportServiceRefundExecutor:
    def __init__(self, support: SupportService) -> None:
        self._support = support

    async def execute(self, actor_id: UUID, order_id: UUID) -> OrderRead:
        from app.domain.support.schemas import RefundCommand

        return await self._support.request_refund(
            RefundCommand(actor_id=actor_id, order_id=order_id)
        )


class SupportServiceEscalator:
    def __init__(self, support: SupportService) -> None:
        self._support = support

    async def escalate(self, actor_id: UUID, reason_code: str, request_id: str) -> TicketRead:
        return await self._support.create_ticket(
            # The request ID is used for an internal correlation-safe subject only.
            # It is not customer text and is bounded by WorkflowRequest.
            CreateTicketCommand(
                actor_id=actor_id,
                subject=f"Workflow escalation ({reason_code}) [{request_id}]",
            )
        )


class TemplateResponseGenerator:
    """A safe response renderer for offline workflow runs."""

    async def generate(
        self,
        message: str,
        route: RoutingDecision,
        evidence: EvidenceBundle,
        proposal: ProposedAction | None,
    ) -> str:
        del message, proposal
        if route.intent is RouteIntent.ORDER_STATUS and evidence.order is not None:
            return f"Your order is currently {evidence.order.status.value}."
        if route.intent is RouteIntent.POLICY and evidence.policy is not None:
            return evidence.policy.content
        return "I could not safely complete this request."


class SupportServiceRefundProposer:
    async def propose(
        self,
        actor_id: UUID,
        request_id: str,
        order: OrderRead,
        policy: PolicyDocumentRead | None,
    ) -> ProposedAction:
        if order.customer_id != actor_id:
            raise Forbidden()
        if order.status not in REFUNDABLE_ORDER_STATUSES:
            raise InvalidTransition()
        return ProposedAction(
            kind="refund",
            proposal_id=uuid4(),
            actor_id=actor_id,
            request_id=request_id,
            order_id=order.id,
            amount=order.total_amount,
            policy_version=policy.version if policy is not None else "unknown",
            created_at=datetime.now(UTC),
        )


def default_dependencies(
    repository: SupportRepository,
    recorder: TraceRecorder,
) -> WorkflowNodeDependencies:
    support = SupportService(repository)
    return WorkflowNodeDependencies(
        router=RuleRouter(),
        order_retriever=SupportServiceOrderRetriever(support),
        policy_retriever=SupportServicePolicyRetriever(support),
        response_generator=TemplateResponseGenerator(),
        refund_proposer=SupportServiceRefundProposer(),
        refund_executor=SupportServiceRefundExecutor(support),
        escalator=SupportServiceEscalator(support),
        recorder=recorder,
    )


class WorkflowService:
    """This class owns workflow IDs, checkpoints, and confirmation authorization."""

    def __init__(
        self,
        dependencies: WorkflowNodeDependencies,
        *,
        checkpointer: Any | None = None,
        clock: Callable[[], datetime] | None = None,
        confirmation_ttl: timedelta = timedelta(minutes=15),
    ) -> None:
        self._dependencies = dependencies
        self._checkpointer = checkpointer or InMemorySaver()
        self._graph: Any = compile_support_graph(dependencies, self._checkpointer)
        self._clock = clock or (lambda: datetime.now(UTC))
        self._confirmation_ttl = confirmation_ttl
        self._locks: dict[str, asyncio.Lock] = {}

    def _lock(self, workflow_id: str) -> asyncio.Lock:
        return self._locks.setdefault(workflow_id, asyncio.Lock())

    def _config(self, workflow_id: str) -> dict[str, dict[str, str]]:
        return {"configurable": {"thread_id": workflow_id}}

    def _response(self, values: dict[str, Any], *, interrupted: bool = False) -> WorkflowResponse:
        return WorkflowResponse(
            workflow_id=str(values["workflow_id"]),
            run_id=str(values["run_id"]),
            workflow_version=str(values.get("workflow_version", WORKFLOW_VERSION)),
            workflow_name=str(values.get("workflow_name", WORKFLOW_ID)),
            status=str(values.get("status", "unknown")),
            state=values,
            interrupted=interrupted,
        )

    async def start(self, request: WorkflowRequest) -> WorkflowResponse:
        workflow_id = str(uuid4())
        run_id = str(uuid4())
        now = self._clock()
        expires_at = request.expires_at or now + self._confirmation_ttl
        state: SupportState = {
            "workflow_id": workflow_id,
            "run_id": run_id,
            "workflow_version": WORKFLOW_VERSION,
            "workflow_name": WORKFLOW_ID,
            "request": request.model_copy(update={"expires_at": expires_at}),
            "status": "received",
            "transcript": (),
        }
        with self._dependencies.recorder.span(
            "support.workflow.interaction",
            {
                "agent.workflow.version": WORKFLOW_VERSION,
                "workflow.id": workflow_id,
                "workflow.run.id": run_id,
            },
        ):
            with self._dependencies.recorder.span(
                "support.workflow.graph",
                {"workflow.id": workflow_id, "workflow.run.id": run_id},
            ):
                result = await self._graph.ainvoke(state, self._config(workflow_id))
        snapshot = await self._graph.aget_state(self._config(workflow_id))
        return self._response(
            snapshot.values,
            interrupted=bool(snapshot.interrupts) or "__interrupt__" in result,
        )

    async def inspect(self, workflow_id: str) -> WorkflowResponse:
        snapshot = await self._graph.aget_state(self._config(workflow_id))
        if not snapshot.values or "workflow_id" not in snapshot.values:
            raise WorkflowNotFound()
        self._check_not_expired(snapshot.values)
        return self._response(snapshot.values, interrupted=bool(snapshot.interrupts))

    def _check_not_expired(self, values: dict[str, Any]) -> None:
        request = values.get("request")
        expires_at = request.expires_at if request is not None else None
        if expires_at is not None and expires_at <= self._clock():
            raise WorkflowExpired()

    async def _resume(
        self,
        workflow_id: str,
        *,
        actor_id: UUID,
        request_id: str,
        decision: str,
    ) -> WorkflowResponse:
        async with self._lock(workflow_id):
            current = await self.inspect(workflow_id)
            values = current.state
            request = cast(WorkflowRequest | None, values.get("request"))
            if request is None or request.actor_id != actor_id:
                raise WorkflowActorMismatch()
            if request.request_id != request_id:
                raise InvalidWorkflowResume("The request ID does not match the workflow.")
            if current.status != "awaiting_confirmation" or not current.interrupted:
                raise InvalidWorkflowResume("The workflow is not waiting for confirmation.")
            with self._dependencies.recorder.span("support.workflow.interaction"):
                with self._dependencies.recorder.span(
                    "support.workflow.graph",
                    {
                        "workflow.id": workflow_id,
                        "workflow.run.id": current.run_id,
                    },
                ):
                    result = await self._graph.ainvoke(
                        Command(
                            resume={
                                "decision": decision,
                                "actor_id": str(actor_id),
                                "request_id": request_id,
                            }
                        ),
                        self._config(workflow_id),
                    )
            snapshot = await self._graph.aget_state(self._config(workflow_id))
            return self._response(
                snapshot.values,
                interrupted=bool(snapshot.interrupts) or "__interrupt__" in result,
            )

    async def confirm(self, workflow_id: str, actor_id: UUID, request_id: str) -> WorkflowResponse:
        return await self._resume(
            workflow_id,
            actor_id=actor_id,
            request_id=request_id,
            decision="confirm",
        )

    async def reject(self, workflow_id: str, actor_id: UUID, request_id: str) -> WorkflowResponse:
        return await self._resume(
            workflow_id,
            actor_id=actor_id,
            request_id=request_id,
            decision="reject",
        )
