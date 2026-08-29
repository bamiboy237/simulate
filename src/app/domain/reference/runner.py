"""This module runs one bounded reference workflow case.

The runner seeds the disposable repository, executes the deterministic agent
plan against the workflow tools, applies the approved fault script at the
tool boundary, retries the SAME faulted call (it never advances to the next
planned call), streams allowlisted events, records mutations, and destroys
the repository. The verdict, business outcome, and metrics are derived by
the workflow observer from the observed final state and mutation trail, and
the observed state transitions are checked against the permitted and
required transition contract. Cleanup is part of the run: a case that leaves
its repository open fails.
"""

import re
from datetime import UTC, datetime
from time import perf_counter
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from app.domain.reference.contracts import (
    ReferenceExpectation,
    ReferenceObservation,
    ReferencePlan,
    ReferenceTool,
    ReferenceToolCall,
    ReferenceWorkflow,
)
from app.domain.simulation.events import (
    SimulationEvent,
    SimulationEventCollector,
    SimulationEventKind,
)

REFERENCE_RUN_SCHEMA_VERSION = "2.0.0"

MAX_RETRIES_PER_CALL = 2

# Captured values look like "pnr=PNRABC123 fare=149.50" inside tool results.
_CAPTURE_PATTERN = re.compile(r"([A-Za-z][A-Za-z0-9_]{1,31})=([^ ;,]+)")


class ReferenceRun(BaseModel):
    """This class stores one normalized reference workflow run.

    The final state and the mutation trail are part of the run, so a report
    can always be checked against the observed evidence.
    """

    model_config = ConfigDict(extra="forbid")

    schema_version: str = Field(
        default=REFERENCE_RUN_SCHEMA_VERSION,
        pattern=r"^\d+\.\d+\.\d+$",
    )
    run_id: str
    workflow_id: str
    side: str = Field(pattern=r"^(baseline|candidate)$")
    label: str = Field(min_length=1, max_length=200)
    verdict: str = Field(
        pattern=r"^(reproduced|accepted|failed|unexpected_access|missing_coverage)$"
    )
    outcome: str = Field(min_length=1, max_length=100)
    reason_code: str = Field(min_length=1, max_length=100)
    gate_verified: bool = False
    safety_ok: bool = True
    events: tuple[SimulationEvent, ...] = ()
    mutations: tuple[dict[str, object], ...] = ()
    transitions: tuple[str, ...] = ()
    tool_calls: tuple[str, ...] = ()
    retries: int = 0
    total_latency_ms: float | None = Field(default=None, ge=0)
    tokens: int = Field(default=0, ge=0)
    cost_usd: float | None = Field(default=None, ge=0)
    errors: tuple[str, ...] = ()
    final_state: dict[str, object] = Field(default_factory=dict)
    final_state_hash: str = Field(min_length=1, max_length=100)
    business_outcome: str = Field(min_length=1, max_length=200)
    business_metrics: dict[str, object] = Field(default_factory=dict)
    tokens_are_estimates: bool = True
    cost_is_estimate: bool = True
    cleanup_ok: bool = False
    completed_at: str = Field(min_length=1, max_length=100)


def _state_hash(state: object) -> str:
    """This function returns a stable fingerprint of one repository state."""
    import hashlib
    import json

    payload = json.dumps(state, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()


def _tool_by_name(workflow: ReferenceWorkflow, name: str) -> ReferenceTool | None:
    for tool in workflow.tools:
        if tool.name == name:
            return tool
    return None


def _transition(mutation: dict[str, object]) -> str | None:
    """This function maps one recorded mutation to its transition string."""
    field = str(mutation.get("field", ""))
    resource = str(mutation.get("resource", ""))
    if field == "created":
        return f"{resource}:created"
    if field == "status":
        return f"{resource}:{mutation.get('before', '?')}->{mutation.get('after', '?')}"
    return None


class _FaultInjection:
    """This class applies the approved fault script once per entry."""

    def __init__(self, workflow: ReferenceWorkflow) -> None:
        script = workflow.fault_script
        self._entries = list(getattr(script, "entries", ())) if script is not None else []
        self._consumed: set[int] = set()

    async def apply(
        self,
        tool_name: str,
        arguments: dict[str, object],
        collector: SimulationEventCollector,
    ) -> None:
        """This method raises or sleeps for one matching unconsumed entry.

        An entry with no declared arguments matches any call of its tool; an
        entry with declared arguments matches only calls whose arguments
        equal them, so a fault can target one specific call of a tool that a
        plan calls several times.
        """
        import asyncio

        for index, entry in enumerate(self._entries):
            if entry.tool != tool_name:
                continue
            declared = getattr(entry, "arguments", {}) or {}
            if declared and any(
                str(arguments.get(key)) != str(value) for key, value in declared.items()
            ):
                continue
            if index in self._consumed and not getattr(entry, "repeat", False):
                continue
            self._consumed.add(index)
            kind = entry.kind.value if hasattr(entry.kind, "value") else str(entry.kind)
            collector.emit(
                SimulationEventKind.FAULT_INJECTED,
                {"fault.kind": kind, "fault.tool": tool_name},
            )
            if kind in ("timeout", "transient_error"):
                raise TimeoutError("fault script timeout")
            await asyncio.sleep(entry.delay_ms / 1000)


def _resolve_arguments(
    call: ReferenceToolCall,
    results: dict[str, dict[str, str]],
) -> dict[str, object]:
    """This function resolves ``$tool.key`` references from captured values."""
    resolved: dict[str, object] = {}
    for key, value in call.arguments.items():
        if isinstance(value, str) and value.startswith("$"):
            reference = value[1:]
            tool_name, _, field = reference.partition(".")
            captured = results.get(tool_name, {})
            if field not in captured:
                raise ValueError(
                    f"unresolved argument {value!r}: {tool_name!r} returned no "
                    f"captured {field!r}"
                )
            resolved[key] = captured[field]
        else:
            resolved[key] = value
    return resolved


def _capture_result(tool_name: str, result: str) -> dict[str, str]:
    """This function extracts ``key=value`` captures from one tool result."""
    return {match.group(1): match.group(2) for match in _CAPTURE_PATTERN.finditer(result)}


def _observed_transitions(mutations: tuple[dict[str, object], ...]) -> tuple[str, ...]:
    """This function returns every state transition the mutation trail shows."""
    transitions = []
    for mutation in mutations:
        transition = _transition(mutation)
        if transition is not None:
            transitions.append(transition)
    return tuple(transitions)


def _check_transitions(
    observed: tuple[str, ...],
    expectation: ReferenceExpectation,
) -> tuple[str, ...]:
    """This function rejects disallowed and missing state transitions."""
    errors: list[str] = []
    allowed = set(expectation.permitted_transitions)
    for transition in observed:
        if transition not in allowed:
            errors.append(f"disallowed state transition {transition!r}")
    missing = [t for t in expectation.required_transitions if t not in observed]
    for transition in missing:
        errors.append(f"missing required state transition {transition!r}")
    return tuple(errors)


async def run_reference_case(
    *,
    workflow: ReferenceWorkflow,
    plan: ReferencePlan,
    side: str,
    label: str,
    collector: SimulationEventCollector | None = None,
) -> ReferenceRun:
    """This function runs one side of one reference workflow case."""
    collector = collector or SimulationEventCollector()
    started = perf_counter()
    repository = workflow.repository
    repository.seed(workflow.seed_state)
    collector.emit(SimulationEventKind.ENVIRONMENT_CREATED, {"environment.id": uuid4().hex[:16]})
    collector.emit(SimulationEventKind.ENVIRONMENT_SEEDED, {"seed.orders": 0})

    tool_calls: list[str] = []
    errors: list[str] = []
    retries = 0
    gate_verified = plan.gate_verified
    safety_ok = True
    tokens = 0
    results: dict[str, dict[str, str]] = {}
    faults = _FaultInjection(workflow)

    cleanup_error: str | None = None
    try:
        index = 0
        attempts_for_index = 0
        while index < len(plan.tool_calls):
            call = plan.tool_calls[index]
            tool = _tool_by_name(workflow, call.tool)
            if tool is None:
                errors.append(f"unknown tool {call.tool!r}")
                safety_ok = False
                break
            collector.emit(SimulationEventKind.TOOL_SELECTED, {"tool": tool.name})
            tool_calls.append(tool.name)
            if (
                not tool.safe
                and workflow.expectation.gate_required
                and not gate_verified
                and tool.name in workflow.expectation.protected_tools
            ):
                errors.append(
                    f"sensitive tool {tool.name!r} used without the approval gate"
                )
                collector.emit(
                    SimulationEventKind.DEPENDENCY_RESULT,
                    {"tool": tool.name, "tool.error.code": "approval_required"},
                )
                safety_ok = False
                break
            try:
                arguments = _resolve_arguments(call, results)
                await faults.apply(tool.name, arguments, collector)
                result = tool.run(repository, arguments)
            except TimeoutError:
                errors.append(f"{tool.name}:timeout")
                collector.emit(
                    SimulationEventKind.DEPENDENCY_RESULT,
                    {"tool": tool.name, "tool.error.code": "timeout"},
                )
                # Retry the SAME planned call: the index does not advance and
                # the attempt counter belongs to this call only, so an earlier
                # successful use of the same tool never eats the retry budget.
                attempts_for_index += 1
                if attempts_for_index <= MAX_RETRIES_PER_CALL:
                    retries += 1
                    collector.emit(SimulationEventKind.RETRY, {"retry.count": retries})
                    continue
                errors.append(f"{tool.name}:retries_exhausted")
                safety_ok = False
                break
            collector.emit(
                SimulationEventKind.DEPENDENCY_RESULT,
                {"tool": tool.name},
            )
            tokens += 18
            captured = _capture_result(tool.name, result)
            if captured:
                results[tool.name] = captured
            attempts_for_index = 0
            index += 1

        final_state = repository.snapshot()
        mutations = repository.mutations()
    finally:
        try:
            repository.destroy()
            collector.emit(
                SimulationEventKind.ENVIRONMENT_DESTROYED,
                {"environment.id": "reference"},
            )
        except Exception as error:
            cleanup_error = str(error)

    transitions = _observed_transitions(mutations)
    try:
        observation = workflow.observer(final_state, mutations)
    except Exception as error:
        observation = ReferenceObservation(
            outcome="failed",
            reason_code="observer_error",
            business_outcome="failed",
            metrics={"error": str(error)[:200]},
        )
        errors.append(f"observer error: {error}")
    transition_errors = _check_transitions(transitions, workflow.expectation)
    errors.extend(transition_errors)

    outcome = observation.outcome
    reason_code = observation.reason_code
    if cleanup_error is not None:
        errors.append(f"cleanup failed: {cleanup_error}")
        verdict = "failed"
        outcome = "failed"
        reason_code = "cleanup_failed"
    elif transition_errors:
        verdict = "failed"
        outcome = "failed"
        reason_code = "transition_contract_violation"
    elif outcome == workflow.expectation.outcome and reason_code in (
        workflow.expectation.reason_codes
    ):
        verdict = "reproduced"
    elif outcome == workflow.expectation.outcome:
        verdict = "accepted"
    else:
        verdict = "failed"

    collector.emit(
        SimulationEventKind.RUN_COMPLETED,
        {
            "run.verdict": verdict,
            "run.retries": retries,
            "run.tokens.total": tokens,
            "run.errors": ",".join(errors),
        },
    )
    return ReferenceRun(
        run_id=uuid4().hex,
        workflow_id=workflow.workflow_id,
        side=side,
        label=label,
        verdict=verdict,
        outcome=outcome,
        reason_code=reason_code,
        gate_verified=gate_verified,
        safety_ok=safety_ok,
        events=collector.events(),
        mutations=mutations,
        transitions=transitions,
        tool_calls=tuple(tool_calls),
        retries=retries,
        total_latency_ms=round((perf_counter() - started) * 1000, 2),
        tokens=tokens,
        cost_usd=round(tokens * 0.00005, 6),
        errors=tuple(errors),
        final_state=dict(final_state) if isinstance(final_state, dict) else {},
        final_state_hash=_state_hash(final_state),
        business_outcome=observation.business_outcome,
        business_metrics=observation.metrics,
        cleanup_ok=cleanup_error is None,
        completed_at=datetime.now(UTC).isoformat(),
    )
