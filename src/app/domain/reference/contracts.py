"""This module defines the bounded reference workflow contract.

A reference workflow is a small agentic system outside the support example:
a stateful repository, safe and sensitive tools, a deterministic agent plan,
one expected outcome, and exactly one declared baseline/candidate variable.
The contract reuses the shared Phase 7 patterns that are domain-neutral:
the event collector and allowlist, the fault-script schema, the comparison
verdicts, and the offline model substitute shape. The support-shaped
contracts (bundle, runner, case library) are intentionally not reshaped;
the integration report flags what their generalization would require.
"""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

Scalar = bool | int | float | str


class ReferenceTool(Protocol):
    """This protocol defines one tool of a reference workflow."""

    name: str
    safe: bool

    def run(self, repository: object, arguments: dict[str, object]) -> str: ...


@runtime_checkable
class ReferenceRepository(Protocol):
    """This protocol defines the disposable state container of one workflow."""

    def seed(self, state: object) -> None: ...

    def snapshot(self) -> object: ...

    def mutations(self) -> tuple[dict[str, object], ...]: ...

    def destroy(self) -> None: ...


class ReferenceExpectation(BaseModel):
    """This class stores what one workflow case must produce.

    ``permitted_transitions`` lists every observed state transition the case
    may make; any observed transition outside it fails the run.
    ``required_transitions`` lists the transitions the case must make; a
    missing one also fails the run.
    """

    model_config = ConfigDict(extra="forbid")

    outcome: str = Field(min_length=1, max_length=100)
    reason_codes: tuple[str, ...] = Field(min_length=1)
    permitted_transitions: tuple[str, ...] = ()
    required_transitions: tuple[str, ...] = ()
    gate_required: bool = False
    gate_tool: str | None = Field(default=None, max_length=100)
    protected_tools: tuple[str, ...] = ()


class ReferenceCandidate(BaseModel):
    """This class stores one declared baseline/candidate variable."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=200)
    change_type: str = Field(min_length=1, max_length=100)
    baseline_label: str = Field(min_length=1, max_length=200)
    candidate_label: str = Field(min_length=1, max_length=200)


class ReferenceToolCall(BaseModel):
    """This class stores one planned tool call of the deterministic agent.

    An argument value shaped ``$<tool>.<key>`` is resolved from the value
    the named earlier tool returned, so later calls use observed identifiers
    (for example the real PNR from the hold step).
    """

    model_config = ConfigDict(extra="forbid")

    tool: str
    arguments: dict[str, object] = Field(default_factory=dict)


class ReferencePlan(BaseModel):
    """This class stores the deterministic agent behavior for one side.

    The plan carries only behavior: the routing decision, the ordered tool
    calls, and whether the approval gate was verified. The outcome, reason,
    business outcome, and metrics are derived by the workflow observer from
    the observed final state and mutation trail, never declared by the plan.
    """

    model_config = ConfigDict(extra="forbid")

    routing: dict[str, object] = Field(default_factory=dict)
    tool_calls: tuple[ReferenceToolCall, ...] = ()
    gate_verified: bool = False


class ReferenceObservation(BaseModel):
    """This class stores what the workflow observer derived from evidence."""

    model_config = ConfigDict(extra="forbid")

    outcome: str = Field(min_length=1, max_length=100)
    reason_code: str = Field(min_length=1, max_length=100)
    business_outcome: str = Field(min_length=1, max_length=200)
    metrics: dict[str, object] = Field(default_factory=dict)


@dataclass(frozen=True)
class ReferenceWorkflow:
    """This class wires one approved reference workflow into the harness.

    The observer derives the outcome, reason, business outcome, and metrics
    from the observed final state and the recorded mutation trail, so no
    workflow can claim success its state did not perform.
    """

    workflow_id: str
    name: str
    source: str
    seed_state: object
    repository: ReferenceRepository
    tools: tuple[ReferenceTool, ...]
    expectation: ReferenceExpectation
    baseline_plan: ReferencePlan
    candidate_plan: ReferencePlan
    candidate: ReferenceCandidate
    observer: "Callable[[object, tuple[dict[str, object], ...]], ReferenceObservation]"
    fault_script: object | None = None
    reused_code: tuple[str, ...] = ()
    integration_note: str = ""
