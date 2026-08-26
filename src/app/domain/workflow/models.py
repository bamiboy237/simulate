"""Typed values shared by the support workflow graph and its API."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import TypedDict
from uuid import UUID

from pydantic import BaseModel, Field

from app.domain.agent.schemas import RoutingDecision, SupportResponse
from app.domain.support.schemas import OrderRead, PolicyDocumentRead

WORKFLOW_VERSION = "4.1.0"
WORKFLOW_ID = "support-controlled-actions"


class WorkflowRequest(BaseModel):
    """A workflow interaction bound to one actor and one caller request."""

    actor_id: UUID
    request_id: str = Field(min_length=1, max_length=200)
    message: str = Field(min_length=1, max_length=2000)
    expires_at: datetime | None = None


class EvidenceBundle(BaseModel):
    """Read-only support evidence gathered before a response or action."""

    order: OrderRead | None = None
    policy: PolicyDocumentRead | None = None


class ProposedAction(BaseModel):
    """A typed action proposal that has not changed support state."""

    kind: str = Field(pattern=r"^[a-z][a-z_]{1,49}$")
    proposal_id: UUID
    actor_id: UUID
    request_id: str
    order_id: UUID
    amount: Decimal = Field(ge=0, decimal_places=2, max_digits=12)
    policy_version: str = Field(min_length=1, max_length=50)
    created_at: datetime


class ConfirmationDecision(StrEnum):
    PENDING = "pending"
    CONFIRM = "confirm"
    REJECT = "reject"


class Confirmation(BaseModel):
    """The checkpoint decision bound to the original actor and request."""

    decision: ConfirmationDecision = ConfirmationDecision.PENDING
    actor_id: UUID
    request_id: str
    decided_at: datetime | None = None


class WorkflowErrorDetail(BaseModel):
    """A safe, typed error recorded in the workflow transcript."""

    code: str = Field(min_length=1, max_length=100)
    message: str = Field(min_length=1, max_length=400)
    retryable: bool = False


class EscalationState(BaseModel):
    """The reason and lifecycle state for a human handoff."""

    reason_code: str = Field(min_length=1, max_length=100)
    status: str = "pending"
    ticket_id: UUID | None = None


class Transition(BaseModel):
    """One observable graph transition."""

    node: str
    status: str
    changed_keys: tuple[str, ...] = ()


class SupportState(TypedDict, total=False):
    """The complete checkpointed state for one support workflow."""

    workflow_id: str
    run_id: str
    workflow_version: str
    workflow_name: str
    request: WorkflowRequest
    route: RoutingDecision
    evidence: EvidenceBundle
    proposed_action: ProposedAction
    confirmation: Confirmation
    response: SupportResponse
    errors: tuple[WorkflowErrorDetail, ...]
    escalation: EscalationState
    transcript: tuple[Transition, ...]
    status: str


class WorkflowResponse(BaseModel):
    """The stable service response for start, inspect, or resume operations."""

    workflow_id: str
    run_id: str
    workflow_version: str
    workflow_name: str
    status: str
    state: dict[str, object]
    interrupted: bool = False

