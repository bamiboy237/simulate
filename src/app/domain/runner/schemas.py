"""This module defines schemas and data models for cloud runners.

All models follow strict validation with extra fields forbidden.
Secrets are encapsulated in SecretStr so values are never leaked in logs,
reprs, or serialization dumps.
"""

from datetime import datetime, timezone
from enum import StrEnum
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator

# Watchdog budget defaults and reserve. The bridge aborts a hung Prime tool after
# DEFAULT_TOOL_TIMEOUT_S and gives it DEFAULT_TOOL_RECOVERY_TIMEOUT_S to recover;
# MODAL_RUN_RESERVE_S covers abort acknowledgment plus final event flush before
# the outer sandbox timeout. Every SandboxSpec must satisfy
#   tool_timeout_s + tool_recovery_timeout_s + MODAL_RUN_RESERVE_S <= outer timeout
# so a tool that starts at run time zero can be aborted, recover (or fail), and
# flush before Modal kills the container. The bridge separately enforces an
# OVERALL run cutoff from the injected outer lifetime
# (SANDBOX_TIMEOUT_S - recovery - reserve) so tools that start late in the run
# (observed live: tool at ~140s of a 300s run) are still aborted in time.
# Together the sum invariant and the run cutoff guarantee deterministic shutdown
# before the Modal kill deadline.
DEFAULT_TOOL_TIMEOUT_S = 600
DEFAULT_TOOL_RECOVERY_TIMEOUT_S = 300
MODAL_RUN_RESERVE_S = 60
DEFAULT_SANDBOX_TIMEOUT_S = 3600

RESERVED_ENV_KEYS: frozenset[str] = frozenset(
    {
        "INVESTIGATION_ID",
        "TASK_BRIEF",
        "CONTROL_PLANE_CALLBACK_URL",
        "BRIDGE_TOKEN",
        "WORLD_GATEWAY_URL",
        "WORLD_GATEWAY_TOKEN",
        "TRACE_REF",
        "TOOL_TIMEOUT_S",
        "TOOL_RECOVERY_TIMEOUT_S",
        "SANDBOX_TIMEOUT_S",
        "SPEC_VERSION",
        "WORLD_ID",
        "WORLD_VERSION",
        "SLICE_NAME",
        "FIXTURE_BUNDLE_REF",
        "SLICE_PROVENANCE",
        "INVESTIGATOR_KIND",
        "INVESTIGATOR_REF",
        "INVESTIGATOR_ENTRYPOINT",
        "TESTED_AGENT_KIND",
        "TESTED_AGENT_REF",
        "TESTED_AGENT_ENTRYPOINT",
    }
)

RESERVED_SECRET_KEYS: frozenset[str] = frozenset(
    {
        "BRIDGE_TOKEN",
    }
)


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
    investigation_id: UUID
    task_brief: str
    environment_slice: EnvironmentSliceRef
    investigator: AgentArtifactRef
    tested_agent: AgentArtifactRef | None = None
    trace_ref: dict[str, Any] | None = None
    env: dict[str, str] = Field(default_factory=dict)
    secrets: dict[str, SecretStr] = Field(default_factory=dict)
    callback_base_url: str
    resource_limits: ResourceLimits = Field(default_factory=ResourceLimits)
    tool_timeout_s: int | None = None
    tool_recovery_timeout_s: int | None = None
    outbound_domain_allowlist: list[str] = Field(default_factory=list)

    @field_validator("env")
    @classmethod
    def validate_reserved_env(cls, env: dict[str, str]) -> dict[str, str]:
        conflicts = RESERVED_ENV_KEYS.intersection(env.keys())
        if conflicts:
            raise ValueError(
                f"Reserved environment variables cannot be overridden: {sorted(conflicts)}"
            )
        return env

    @model_validator(mode="after")
    def validate_and_resolve_watchdog_budget(self) -> "SandboxSpec":
        """Resolve and enforce the watchdog budget against the outer timeout.

        The watchdog must always fire, recover (or fail), and flush before the
        sandbox's outer lifetime. Enforce
        ``tool_timeout_s + tool_recovery_timeout_s + MODAL_RUN_RESERVE_S``
        against ``resource_limits.timeout_s`` (or the runner default when that
        is non-positive), with positive values only. This is authoritative for
        every SandboxSpec, including the 300s live smoke configuration.
        """
        tool_timeout = (
            self.tool_timeout_s
            if self.tool_timeout_s is not None
            else DEFAULT_TOOL_TIMEOUT_S
        )
        recovery_timeout = (
            self.tool_recovery_timeout_s
            if self.tool_recovery_timeout_s is not None
            else DEFAULT_TOOL_RECOVERY_TIMEOUT_S
        )
        outer = (
            self.resource_limits.timeout_s
            if self.resource_limits.timeout_s > 0
            else DEFAULT_SANDBOX_TIMEOUT_S
        )

        if tool_timeout <= 0 or recovery_timeout <= 0:
            raise ValueError(
                "watchdog timeouts must be positive: "
                f"TOOL_TIMEOUT_S={tool_timeout}, "
                f"TOOL_RECOVERY_TIMEOUT_S={recovery_timeout}"
            )
        if tool_timeout + recovery_timeout + MODAL_RUN_RESERVE_S > outer:
            raise ValueError(
                "watchdog budget exceeds the sandbox outer timeout: "
                f"TOOL_TIMEOUT_S={tool_timeout} + "
                f"TOOL_RECOVERY_TIMEOUT_S={recovery_timeout} + "
                f"MODAL_RUN_RESERVE_S={MODAL_RUN_RESERVE_S} = "
                f"{tool_timeout + recovery_timeout + MODAL_RUN_RESERVE_S}s "
                f"> resource_limits.timeout_s={outer}s. Lower the watchdog "
                "timeouts or raise the sandbox timeout so the watchdog can "
                "abort, recover, and flush before Modal kills the container."
            )

        self.tool_timeout_s = tool_timeout
        self.tool_recovery_timeout_s = recovery_timeout
        return self


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
