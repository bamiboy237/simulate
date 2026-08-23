"""Fake cloud runner for fast, offline unit and integration testing."""

from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from app.domain.runner.base import CloudRunner
from app.domain.runner.schemas import (
    RunnerState,
    RunnerStatus,
    SandboxHandle,
    SandboxSpec,
)


class FakeRunner(CloudRunner):
    """Test double implementing CloudRunner without external dependencies."""

    def __init__(
        self,
        *,
        create_error: Exception | None = None,
        status_error: Exception | None = None,
        terminate_error: Exception | None = None,
        default_initial_state: RunnerState = RunnerState.running,
    ) -> None:
        self.specs: list[SandboxSpec] = []
        self.handles: list[SandboxHandle] = []
        self.terminated_handles: list[SandboxHandle] = []
        self.terminated_sandboxes: list[str] = []
        self.statuses: dict[str, RunnerStatus] = {}
        self.scripted_statuses: dict[str, list[RunnerStatus]] = {}
        self._next_sandbox_id: str | None = None
        self.create_error = create_error
        self.status_error = status_error
        self.terminate_error = terminate_error
        self.default_initial_state = default_initial_state

    def set_next_sandbox_id(self, sandbox_id: str) -> None:
        """Configure the sandbox ID to return on the next create_sandbox call."""
        self._next_sandbox_id = sandbox_id

    def create_sandbox(self, spec: SandboxSpec) -> SandboxHandle:
        """Record spec and return a canned SandboxHandle."""
        if self.create_error is not None:
            raise self.create_error

        self.specs.append(spec)
        sandbox_id = self._next_sandbox_id or f"sbx_fake_{len(self.specs)}_{uuid4().hex[:8]}"
        self._next_sandbox_id = None
        handle = SandboxHandle(
            sandbox_id=sandbox_id,
            provider="fake",
            created_at=datetime.now(timezone.utc),
        )
        self.handles.append(handle)
        self.statuses[sandbox_id] = RunnerStatus(
            sandbox_id=sandbox_id,
            state=self.default_initial_state,
        )
        return handle

    def get_status(self, handle: SandboxHandle) -> RunnerStatus:
        """Return current status or consume the next scripted status."""
        if self.status_error is not None:
            raise self.status_error

        sandbox_id = handle.sandbox_id
        if sandbox_id in self.scripted_statuses and self.scripted_statuses[sandbox_id]:
            next_status = self.scripted_statuses[sandbox_id].pop(0)
            self.statuses[sandbox_id] = next_status
            return next_status

        return self.statuses.get(
            sandbox_id,
            RunnerStatus(sandbox_id=sandbox_id, state=RunnerState.unknown),
        )

    def terminate(self, handle: SandboxHandle) -> None:
        """Record termination and set status to TERMINATED. Safe to call multiple times."""
        if self.terminate_error is not None:
            raise self.terminate_error

        self.terminated_handles.append(handle)
        if handle.sandbox_id not in self.terminated_sandboxes:
            self.terminated_sandboxes.append(handle.sandbox_id)
        self.statuses[handle.sandbox_id] = RunnerStatus(
            sandbox_id=handle.sandbox_id,
            state=RunnerState.terminated,
        )

    def set_status(
        self,
        sandbox_id: str,
        state: RunnerState,
        *,
        exit_code: int | None = None,
        detail: dict[str, Any] | None = None,
    ) -> None:
        """Explicitly set the current status of a sandbox."""
        self.statuses[sandbox_id] = RunnerStatus(
            sandbox_id=sandbox_id,
            state=state,
            exit_code=exit_code,
            detail=detail or {},
        )

    def script_statuses(
        self,
        sandbox_id: str,
        sequence: list[RunnerStatus | RunnerState],
    ) -> None:
        """Script a progression of statuses returned by subsequent get_status calls."""
        normalized: list[RunnerStatus] = []
        for item in sequence:
            if isinstance(item, RunnerState):
                normalized.append(RunnerStatus(sandbox_id=sandbox_id, state=item))
            else:
                normalized.append(item)
        self.scripted_statuses[sandbox_id] = normalized

    def is_terminated(self, sandbox_id: str) -> bool:
        """Check if a specific sandbox has been terminated."""
        return sandbox_id in self.terminated_sandboxes

    def reset(self) -> None:
        """Reset internal recorder state."""
        self.specs.clear()
        self.handles.clear()
        self.terminated_handles.clear()
        self.terminated_sandboxes.clear()
        self.statuses.clear()
        self.scripted_statuses.clear()
        self._next_sandbox_id = None
