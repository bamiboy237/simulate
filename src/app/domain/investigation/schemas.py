"""Pydantic models and schemas for investigation APIs and domain services."""

from datetime import datetime
from enum import StrEnum
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, SecretStr

from app.domain.runner.schemas import (
    AgentArtifactRef,
    EnvironmentSliceRef,
    ResourceLimits,
)


class InvestigationStatus(StrEnum):
    """Lifecycle statuses for an investigation run."""

    PENDING = "pending"
    PROVISIONING = "provisioning"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"

TERMINAL_STATUSES: frozenset[InvestigationStatus] = frozenset(
    {
        InvestigationStatus.COMPLETED,
        InvestigationStatus.FAILED,
        InvestigationStatus.CANCELLED,
    }
)


class InvestigationCreateRequest(BaseModel):
    """Payload to create and launch a new investigation."""

    model_config = ConfigDict(extra="forbid")

    trace_ref: dict[str, Any]
    world_ref: dict[str, Any]
    slice_ref: EnvironmentSliceRef
    investigator_ref: AgentArtifactRef
    tested_agent_ref: AgentArtifactRef | None = None
    task_brief: str | None = None
    env: dict[str, str] = Field(default_factory=dict)
    secrets: dict[str, SecretStr] = Field(default_factory=dict)
    resource_limits: ResourceLimits = Field(default_factory=ResourceLimits)


class InvestigationResponse(BaseModel):
    """Standard API representation of an investigation."""

    model_config = ConfigDict(extra="forbid")

    id: UUID
    status: InvestigationStatus
    task_brief: str
    bridge_token: str | None = None
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    error: str | None = None


class InvestigationSummaryResponse(BaseModel):
    """Persisted findings summary."""

    model_config = ConfigDict(extra="forbid")

    id: int
    investigation_id: UUID
    findings: str
    next_step: str
    evidence_refs: list[str | dict[str, Any]]
    created_at: datetime


class InvestigationDetailResponse(BaseModel):
    """Detailed view of an investigation including summary if complete."""

    model_config = ConfigDict(extra="forbid")

    id: UUID
    status: InvestigationStatus
    task_brief: str
    trace_ref: dict[str, Any]
    world_ref: dict[str, Any]
    slice_ref: dict[str, Any]
    investigator_ref: dict[str, Any]
    tested_agent_ref: dict[str, Any] | None = None
    error: str | None = None
    last_heartbeat_at: datetime | None = None
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    summary: InvestigationSummaryResponse | None = None


class ChatMessageRequest(BaseModel):
    """Payload to send a chat message to a running investigator."""

    model_config = ConfigDict(extra="forbid")

    body: str
    mode: Literal["prompt", "steer", "follow_up"] = "prompt"


class ChatMessageResponse(BaseModel):
    """Persisted chat message."""

    model_config = ConfigDict(extra="forbid")

    id: int
    investigation_id: UUID
    sender: str
    body: str
    mode: str | None = None
    created_at: datetime


class InboxMessagesResponse(BaseModel):
    """Response returned to bridge during long-poll inbox check."""

    model_config = ConfigDict(extra="forbid")

    messages: list[ChatMessageResponse]
    next_cursor: int
