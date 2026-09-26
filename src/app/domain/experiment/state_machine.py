"""Lifecycle rules and safe error codes for durable support experiments."""

import re
from enum import StrEnum


class ExperimentStatus(StrEnum):
    """The lifecycle states for one persisted experiment."""

    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class IterationOutcomeStatus(StrEnum):
    """The terminal outcome recorded for one scheduled iteration."""

    COMPLETED = "completed"
    FAILED = "failed"


TERMINAL_EXPERIMENT_STATUSES: frozenset[ExperimentStatus] = frozenset(
    {
        ExperimentStatus.COMPLETED,
        ExperimentStatus.FAILED,
        ExperimentStatus.CANCELLED,
    }
)

ALLOWED_EXPERIMENT_TRANSITIONS: dict[ExperimentStatus, frozenset[ExperimentStatus]] = {
    ExperimentStatus.PENDING: frozenset(
        {
            ExperimentStatus.RUNNING,
            ExperimentStatus.FAILED,
            ExperimentStatus.CANCELLED,
        }
    ),
    ExperimentStatus.RUNNING: frozenset(
        {
            ExperimentStatus.COMPLETED,
            ExperimentStatus.FAILED,
            ExperimentStatus.CANCELLED,
        }
    ),
    ExperimentStatus.COMPLETED: frozenset(),
    ExperimentStatus.FAILED: frozenset(),
    ExperimentStatus.CANCELLED: frozenset(),
}

SAFE_ERROR_CODE_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,99}$")


class ExperimentPersistenceError(ValueError):
    """Raised when persisted experiment data violates the durable contract."""


class ExperimentConflictError(ExperimentPersistenceError):
    """Raised when a stale operation conflicts with immutable persisted state."""


class ExperimentNotFoundError(ExperimentPersistenceError):
    """Raised when an experiment ID does not identify a persisted experiment."""


class InvalidExperimentTransitionError(ExperimentPersistenceError):
    """Raised when a lifecycle transition is not permitted."""


class TerminalExperimentImmutableError(ExperimentPersistenceError):
    """Raised when code attempts to modify a terminal experiment."""


def require_transition(current: ExperimentStatus, target: ExperimentStatus) -> None:
    """Reject transitions outside the one durable experiment lifecycle."""
    if current in TERMINAL_EXPERIMENT_STATUSES:
        raise TerminalExperimentImmutableError(
            f"experiment is terminal in {current.value!r} and cannot be modified"
        )
    if target not in ALLOWED_EXPERIMENT_TRANSITIONS[current]:
        raise InvalidExperimentTransitionError(
            f"cannot transition experiment from {current.value!r} to {target.value!r}"
        )


def require_safe_error_code(error_code: str) -> None:
    """Reject unbounded exception text in persisted experiment error fields."""
    if SAFE_ERROR_CODE_PATTERN.fullmatch(error_code) is None:
        raise ExperimentPersistenceError(
            "error_code must be a lowercase identifier containing only letters, digits, and "
            "underscores"
        )
