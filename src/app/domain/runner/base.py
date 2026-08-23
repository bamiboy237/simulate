"""This module defines the CloudRunner protocol for sandbox management."""

from typing import Protocol, runtime_checkable

from app.domain.runner.schemas import RunnerStatus, SandboxHandle, SandboxSpec


@runtime_checkable
class CloudRunner(Protocol):
    """Protocol for cloud sandbox execution providers.

    Swapping providers never touches the chat or event path, as events flow
    from bridge to control plane over HTTP and chat flows the reverse route.
    """

    def create_sandbox(self, spec: SandboxSpec) -> SandboxHandle:
        """Create and launch a detached sandbox for the given specification."""
        ...

    def get_status(self, handle: SandboxHandle) -> RunnerStatus:
        """Get the current operational status of the sandbox."""
        ...

    def terminate(self, handle: SandboxHandle) -> None:
        """Terminate the running sandbox. Safe to call multiple times."""
        ...
