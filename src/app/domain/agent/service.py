"""This module provides deterministic guards for the reference agent's tools.

This module protects customers and orders with ownership checks.
This module checks refund eligibility and records policy versions.
This module requires explicit confirmation before it executes a refund.
The model chooses tool arguments.
The model cannot bypass these rules.
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from time import perf_counter
from typing import Literal, TypeVar
from uuid import UUID, uuid4

from app.domain.agent.errors import RefundNotConfirmed
from app.domain.agent.instructions import ACCEPTED_POLICY_VERSION, POLICY_SLUG
from app.domain.agent.schemas import (
    AnswerContext,
    Escalation,
    ReasonCode,
    RefundProposal,
    RouteIntent,
    RoutingDecision,
    SupportOutcome,
    SupportResponse,
)
from app.domain.retrieval.contracts import RetrievalHit, Retriever
from app.domain.support.errors import Forbidden, InvalidTransition, OrderNotFound
from app.domain.support.schemas import (
    CreateTicketCommand,
    OrderRead,
    PolicyDocumentRead,
    RefundCommand,
    TicketRead,
)
from app.domain.support.service import REFUNDABLE_ORDER_STATUSES, SupportService
from app.telemetry.recorder import TraceRecorder

T = TypeVar("T")

TOOLS_BY_INTENT: dict[RouteIntent, tuple[str, ...]] = {
    RouteIntent.ORDER_STATUS: ("get_order_status", "escalate"),
    RouteIntent.POLICY: ("get_policy", "escalate"),
    RouteIntent.REFUND: ("get_order_status", "propose_refund", "confirm_refund", "escalate"),
    RouteIntent.ESCALATE: (),
}


@dataclass(frozen=True)
class RoutingPlan:
    """This class stores the next action that the orchestrator selects after routing."""

    action: Literal["answer", "escalate"]
    reason_code: ReasonCode


def plan_turn(routing: RoutingDecision) -> RoutingPlan:
    """This function decides the next step from the routing decision alone.

    If a request needs an order reference and lacks one, this function escalates the request.
    The function makes no further model call.
    """
    if routing.intent is RouteIntent.ESCALATE:
        return RoutingPlan(action="escalate", reason_code=ReasonCode.ESCALATED)
    if routing.intent is RouteIntent.ORDER_STATUS and routing.order_id is None:
        return RoutingPlan(action="escalate", reason_code=ReasonCode.ESCALATED_MISSING_REFERENCE)
    if routing.intent is RouteIntent.REFUND and routing.order_id is None:
        return RoutingPlan(
            action="escalate", reason_code=ReasonCode.REFUND_BLOCKED_MISSING_REFERENCE
        )
    return RoutingPlan(action="answer", reason_code=ReasonCode.ORDER_STATUS_OK)


def _error_code(error: Exception) -> str:
    """This function maps an exception to a stable reason code for traces."""
    if isinstance(error, TimeoutError):
        return ReasonCode.TIMEOUT.value
    if isinstance(error, RefundNotConfirmed):
        return error.code
    return getattr(error, "code", "unknown")


class SupportAgentService:
    """This class provides tools for one authenticated customer."""

    def __init__(
        self,
        support_service: SupportService,
        customer_id: UUID,
        recorder: TraceRecorder,
        refund_confirmed: bool = False,
        policy_retriever: Retriever | None = None,
    ) -> None:
        self._support = support_service
        self.customer_id = customer_id
        self._recorder = recorder
        self._refund_confirmed = refund_confirmed
        self._policy_retriever = policy_retriever
        self._pending_proposals: dict[UUID, RefundProposal] = {}
        self._confirmed_order_ids: set[UUID] = set()
        self.tool_calls: list[str] = []
        self.retrieved_policy_version: str | None = None
        self.last_tool_error: ReasonCode | None = None
        self.last_order: OrderRead | None = None
        self.last_policy: PolicyDocumentRead | None = None
        self.last_escalation: Escalation | None = None
        self.last_policy_hits: list[RetrievalHit] = []
        self._request_message = ""

    @property
    def policy_retriever(self) -> Retriever | None:
        """Return the optional measured policy retriever."""
        return self._policy_retriever

    def set_request_message(self, message: str) -> None:
        """Set the user query used by the policy retrieval tool."""
        self._request_message = message

    def record_tool_call(self, name: str) -> None:
        """This method records one tool execution for grounding and efficiency checks."""
        self.tool_calls.append(name)

    def pending_proposal(self, order_id: UUID | None) -> RefundProposal | None:
        """This method returns the pending proposal for one order.

        If no proposal exists, this method returns ``None``.
        """
        if order_id is None:
            return None
        return self._pending_proposals.get(order_id)

    def confirmed_refund_for(self, order_id: UUID | None) -> OrderRead | None:
        """This method returns the confirmed refund result for one order.

        If no result exists, this method returns ``None``.
        """
        if order_id is None or order_id not in self._confirmed_order_ids:
            return None
        return (
            self.last_order
            if self.last_order is not None and self.last_order.id == order_id
            else None
        )

    async def _database_call(self, operation: str, call: Callable[[], Awaitable[T]]) -> T:
        with self._recorder.span("support_agent.database.read") as span:
            span.set_attribute("db.operation", operation)
            started_at = perf_counter()
            try:
                return await call()
            except Exception as error:
                span.set_attribute("db.error.code", _error_code(error))
                raise
            finally:
                span.set_attribute(
                    "db.latency.ms",
                    round((perf_counter() - started_at) * 1000, 2),
                )

    async def get_order_status(self, order_id: UUID) -> OrderRead:
        """This method returns the order after the bound customer passes the ownership check.

        If the bound customer fails the check, the support service raises an error.
        """
        order = await self._database_call(
            "get_order",
            lambda: self._support.get_order(order_id, self.customer_id),
        )
        self.last_order = order
        return order

    async def get_policy(self, slug: str = POLICY_SLUG) -> PolicyDocumentRead:
        """Retrieve the policy and, when configured, exact evidence for the query."""
        with self._recorder.span("support_agent.retrieval.policy") as span:
            span.set_attribute("retrieval.source", "policy_documents")
            span.set_attribute("retrieval.policy.slug", slug)
            policy = await self._database_call(
                "get_policy",
                lambda: self._support.get_latest_policy(slug),
            )
            assert isinstance(policy, PolicyDocumentRead)
            span.set_attribute("retrieval.policy.version", policy.version)
            self.retrieved_policy_version = policy.version
            self.last_policy = policy
            if self._policy_retriever is not None:
                self.last_policy_hits = await self._policy_retriever.search(self._request_message)
            return policy

    async def propose_refund(self, order_id: UUID, reason: str) -> RefundProposal:
        """This method creates a refund proposal for an order.

        If the customer does not own the order, the service raises an error.
        If the order cannot receive a refund, the service raises an error.
        If both checks pass, the service creates the proposal.
        The service creates no proposal after either error.
        The public tool keeps ``reason`` in its contract for the model.
        The trace never stores ``reason``.
        """
        try:
            return await self._propose_refund(order_id)
        except Forbidden:
            self.last_tool_error = ReasonCode.REFUND_BLOCKED_FORBIDDEN
            raise
        except OrderNotFound:
            self.last_tool_error = ReasonCode.ORDER_NOT_FOUND
            raise
        except InvalidTransition:
            self.last_tool_error = ReasonCode.REFUND_BLOCKED_INELIGIBLE
            raise

    async def _propose_refund(self, order_id: UUID) -> RefundProposal:
        order = await self.get_order_status(order_id)
        if order.status not in REFUNDABLE_ORDER_STATUSES:
            with self._recorder.span("support_agent.policy.check") as span:
                span.set_attribute("policy.version", ACCEPTED_POLICY_VERSION)
                span.set_attribute("policy.decision", "denied")
                span.set_attribute("policy.reason.code", ReasonCode.REFUND_BLOCKED_INELIGIBLE.value)
            self.last_tool_error = ReasonCode.REFUND_BLOCKED_INELIGIBLE
            raise InvalidTransition()
        with self._recorder.span("support_agent.policy.check") as span:
            policy_version = self.retrieved_policy_version or ACCEPTED_POLICY_VERSION
            span.set_attribute("policy.version", policy_version)
            span.set_attribute("policy.decision", "allowed")
        proposal = RefundProposal(
            proposal_id=uuid4(),
            order_id=order.id,
            customer_id=self.customer_id,
            amount=order.total_amount,
            policy_version=policy_version,
        )
        self._pending_proposals[order.id] = proposal
        with self._recorder.span("support_agent.confirmation") as span:
            span.set_attribute("confirmation.required", True)
            span.set_attribute("confirmation.verified", False)
        self.last_tool_error = None
        return proposal

    async def confirm_refund(self, order_id: UUID) -> OrderRead:
        """Execute a proposed refund only when the trusted request confirms it."""
        proposal = self._pending_proposals.get(order_id)
        if proposal is None or not self._refund_confirmed:
            with self._recorder.span("support_agent.confirmation") as span:
                span.set_attribute("confirmation.required", True)
                span.set_attribute("confirmation.verified", False)
            self.last_tool_error = ReasonCode.REFUND_BLOCKED_UNCONFIRMED
            raise RefundNotConfirmed()
        with self._recorder.span("support_agent.confirmation") as span:
            span.set_attribute("confirmation.required", True)
            span.set_attribute("confirmation.verified", True)
            refunded = await self._database_call(
                "save_order",
                lambda: self._support.request_refund(
                    RefundCommand(actor_id=self.customer_id, order_id=order_id)
                ),
            )
        self._pending_proposals.pop(order_id, None)
        self._confirmed_order_ids.add(order_id)
        self.last_order = refunded
        self.last_tool_error = None
        return refunded

    async def escalate(self, subject: str) -> TicketRead:
        """This method creates a ticket for human escalation for the bound customer."""
        with self._recorder.span("support_agent.escalation") as span:
            ticket = await self._support.create_ticket(
                CreateTicketCommand(actor_id=self.customer_id, subject=subject)
            )
            span.set_attribute("escalation.ticket.id", str(ticket.id))
            span.set_attribute("escalation.reason.code", ReasonCode.ESCALATED.value)
            self.last_escalation = Escalation(
                ticket_id=ticket.id,
                reason_code=ReasonCode.ESCALATED,
            )
            return ticket


async def escalate_turn(
    service: SupportAgentService,
    routing: RoutingDecision,
    reason_code: ReasonCode,
) -> SupportResponse:
    """This function escalates a turn without another model call."""
    subject = f"Escalated support request ({reason_code.value})"
    ticket = await service.escalate(subject)
    context = AnswerContext(
        routing=routing,
        escalation=Escalation(ticket_id=ticket.id, reason_code=reason_code),
    )
    if reason_code is ReasonCode.ESCALATED_MISSING_REFERENCE:
        message = (
            "We could not find an order identifier in your request. "
            "A human support agent will contact you shortly."
        )
    elif reason_code is ReasonCode.REFUND_BLOCKED_MISSING_REFERENCE:
        message = (
            "We could not find an order identifier for your refund request. "
            "A human support agent will contact you shortly."
        )
    else:
        message = (
            "I have handed this request to a human support agent, who will "
            "follow up with you shortly."
        )
    return SupportResponse(
        intent=RouteIntent.ESCALATE,
        outcome=SupportOutcome.ESCALATED,
        reason_code=reason_code,
        message=message,
        context=context,
    )
