"""This package defines cloud runner contracts, schemas, and interfaces."""

from typing import TYPE_CHECKING

from app.domain.runner.base import CloudRunner
from app.domain.runner.schemas import (
    DEFAULT_TOOL_RECOVERY_TIMEOUT_S,
    DEFAULT_TOOL_TIMEOUT_S,
    MODAL_RUN_RESERVE_S,
    RESERVED_ENV_KEYS,
    RESERVED_SECRET_KEYS,
    AgentArtifactRef,
    ChatEnvelope,
    EnvironmentSliceRef,
    EventType,
    ResourceLimits,
    RunnerEvent,
    RunnerState,
    RunnerStatus,
    SandboxHandle,
    SandboxSpec,
)

if TYPE_CHECKING:
    from app.domain.runner.modal_runner import ModalRunner


def __getattr__(name: str) -> object:
    """Load the Modal adapter only in control-plane processes that request it."""
    if name == "ModalRunner":
        from app.domain.runner.modal_runner import ModalRunner

        return ModalRunner
    raise AttributeError(name)


__all__ = [
    "AgentArtifactRef",
    "ChatEnvelope",
    "CloudRunner",
    "DEFAULT_TOOL_RECOVERY_TIMEOUT_S",
    "DEFAULT_TOOL_TIMEOUT_S",
    "EnvironmentSliceRef",
    "EventType",
    "ModalRunner",
    "MODAL_RUN_RESERVE_S",
    "RESERVED_ENV_KEYS",
    "RESERVED_SECRET_KEYS",
    "ResourceLimits",
    "RunnerEvent",
    "RunnerState",
    "RunnerStatus",
    "SandboxHandle",
    "SandboxSpec",
]
