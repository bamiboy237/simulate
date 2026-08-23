"""This module defines schemas and data models for cloud runners.

All models follow strict validation with extra fields forbidden.
Secrets are encapsulated in SecretStr so values are never leaked in logs,
reprs, or serialization dumps.
"""

from datetime import datetime, timezone
from enum import StrEnum
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, SecretStr


class EnvironmentSliceRef(BaseModel):
    """Reference to an environment slice for reproduction."""

    model_config = ConfigDict(extra="forbid")

    world_id: str
    world_version: str
    slice_name: str
    fixture_bundle_ref: str
    provenance: Literal["observed", "generated", "human-approved"]


class AgentArtifactRef(BaseModel):
    """Reference to an agent artifact (OCI digest or Prime profile)."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["oci_digest", "prime_profile"]
    digest_or_profile: str
    entrypoint: str | None = None


class ResourceLimits(BaseModel):
    """Resource limits enforced on the sandbox container."""

    model_config = ConfigDict(extra="forbid")

    timeout_s: int = 3600
    cpus: int = 1
    memory_mib: int = 2048


class SandboxSpec(BaseModel):
    """Specification required to create a new sandbox environment."""

    model_config = ConfigDict(extra="forbid")

    spec_version: str = "1.0.0"
    task_brief: str
    environment_slice: EnvironmentSliceRef
    investigator: AgentArtifactRef
    tested_agent: AgentArtifactRef | None = None
    env: dict[str, str] = Field(default_factory=dict)
    secrets: dict[str, SecretStr] = Field(default_factory=dict)
    callback_base_url: str
    resource_limits: ResourceLimits = Field(default_factory=ResourceLimits)


class SandboxHandle(BaseModel):
    """Handle referencing a running or provisioned sandbox."""

    model_config = ConfigDict(extra="forbid")

    sandbox_id: str
    provider: str
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class RunnerState(StrEnum):
    """Operational lifecycle state of a runner sandbox."""

    pending = "pending"
    provisioning = "provisioning"
    running = "running"
    completed = "completed"
    failed = "failed"
    terminated = "terminated"
    unknown = "unknown"


class RunnerStatus(BaseModel):
    """Status snapshot of a cloud runner sandbox."""

    model_config = ConfigDict(extra="forbid")

    sandbox_id: str
    state: RunnerState
    exit_code: int | None = None
    error: str | None = None
    detail: dict[str, Any] = Field(default_factory=dict)


class EventType(StrEnum):
    """Enumeration of event types emitted during an investigation."""

    investigation_started = "investigation_started"
    agent_ready = "agent_ready"
    thought = "thought"
    tool_call = "tool_call"
    tool_result = "tool_result"
    message = "message"
    state_diff = "state_diff"
    evaluator_verdict = "evaluator_verdict"
    chat_from_user = "chat_from_user"
    chat_ack = "chat_ack"
    summary_submitted = "summary_submitted"
    warning = "warning"
    error = "error"
    heartbeat = "heartbeat"
    investigation_finished = "investigation_finished"


class RunnerEvent(BaseModel):
    """An event emitted by the runner or sandbox bridge."""

    model_config = ConfigDict(extra="forbid")

    investigation_id: UUID
    seq: int
    type: EventType
    payload: dict[str, Any] = Field(default_factory=dict)
    emitted_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class ChatEnvelope(BaseModel):
    """Envelope for user chat messages directed at the runner/investigator."""

    model_config = ConfigDict(extra="forbid")

    message_id: UUID
    body: str
    mode: Literal["prompt", "steer", "follow_up"]
