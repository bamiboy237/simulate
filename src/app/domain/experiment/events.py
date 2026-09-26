"""Typed event contracts for future experiment execution.

These event types are intentionally independent from investigation runner
events: an experiment records baseline/candidate iteration progress rather
than a human-guided investigation transcript.
"""

from datetime import datetime
from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.domain.experiment.contracts import IterationIdentity


class ExperimentEventKind(StrEnum):
    """The event kinds meaningful to a controlled experiment."""

    PLAN_CREATED = "experiment_plan_created"
    ITERATION_STARTED = "experiment_iteration_started"
    ITERATION_COMPLETED = "experiment_iteration_completed"
    ITERATION_FAILED = "experiment_iteration_failed"
    LIMIT_REACHED = "experiment_limit_reached"
    EXPERIMENT_COMPLETED = "experiment_completed"
    EXPERIMENT_FAILED = "experiment_failed"


ITERATION_EVENT_KINDS: frozenset[ExperimentEventKind] = frozenset(
    {
        ExperimentEventKind.ITERATION_STARTED,
        ExperimentEventKind.ITERATION_COMPLETED,
        ExperimentEventKind.ITERATION_FAILED,
    }
)


class ExperimentEvent(BaseModel):
    """One sequence-numbered experiment event with an optional iteration identity."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    experiment_id: UUID
    execution_id: UUID
    sequence: int = Field(ge=1)
    kind: ExperimentEventKind
    emitted_at: datetime
    iteration: IterationIdentity | None = None

    @model_validator(mode="after")
    def validate_iteration_scope(self) -> "ExperimentEvent":
        """Require an iteration only for the events that describe one iteration."""
        requires_iteration = self.kind in ITERATION_EVENT_KINDS
        if requires_iteration and self.iteration is None:
            raise ValueError(f"{self.kind.value!r} requires an iteration identity")
        if not requires_iteration and self.iteration is not None:
            raise ValueError(f"{self.kind.value!r} must not include an iteration identity")
        if self.iteration is not None and self.iteration.experiment_id != self.experiment_id:
            raise ValueError("event iteration belongs to a different experiment")
        return self
