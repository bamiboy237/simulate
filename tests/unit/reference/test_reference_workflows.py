"""This module tests the Phase 7.6 reference workflow harness (Round 2).

Regression coverage for Inspector's blockers: every workflow mutates its
repository and derives its verdict and business outcome from the observed
final state; flight booking confirms with the real held PNR; a faulted tool
call is retried as the SAME call (and exhausted retries fail); the permitted
and required state-transition contract is enforced against the mutation
trail.
"""

import dataclasses

import pytest

from app.domain.comparison.compare import ComparisonVerdict
from app.domain.reference.compare import compare_reference_runs
from app.domain.reference.contracts import (
    ReferenceExpectation,
    ReferencePlan,
    ReferenceToolCall,
)
from app.domain.reference.runner import MAX_RETRIES_PER_CALL, run_reference_case
from app.domain.reference.workflows.repo import InMemoryReferenceRepository
from app.domain.reference.workflows.six_reference import ALL_WORKFLOWS
from app.domain.simulation.faults import FaultKind, FaultScript, FaultScriptEntry

WORKFLOW_IDS = [workflow.workflow_id for workflow in ALL_WORKFLOWS]


def _workflow(workflow_id: str):
    return next(w for w in ALL_WORKFLOWS if w.workflow_id == workflow_id)


@pytest.mark.parametrize("workflow_id", WORKFLOW_IDS)
async def test_each_workflow_is_stateful_and_derives_outcome_from_state(
    workflow_id: str,
) -> None:
    """A workflow that claims a transition must record it and observe it."""
    workflow = _workflow(workflow_id)

    baseline = await run_reference_case(
        workflow=workflow,
        plan=workflow.baseline_plan,
        side="baseline",
        label=workflow.candidate.baseline_label,
    )
    candidate = await run_reference_case(
        workflow=workflow,
        plan=workflow.candidate_plan,
        side="candidate",
        label=workflow.candidate.candidate_label,
    )
    comparison = compare_reference_runs(
        workflow_id=workflow.workflow_id,
        change_type=workflow.candidate.change_type,
        baseline_label=workflow.candidate.baseline_label,
        candidate_label=workflow.candidate.candidate_label,
        baseline=baseline,
        candidate=candidate,
    )

    assert baseline.mutations, (
        f"{workflow_id} baseline claims success without a mutation trail"
    )
    assert baseline.transitions, (
        f"{workflow_id} baseline claims a state transition that never happened"
    )
    assert set(workflow.expectation.required_transitions) <= set(baseline.transitions), (
        f"{workflow_id} baseline missing required transitions: "
        f"{baseline.transitions}"
    )
    assert baseline.verdict == "reproduced", f"{workflow_id} baseline: {baseline.errors}"
    assert baseline.cleanup_ok is True
    assert baseline.safety_ok is True
    assert baseline.business_outcome != "failed"
    assert candidate.verdict == "failed", f"{workflow_id} candidate: {candidate.errors}"
    assert comparison.verdict is ComparisonVerdict.CANDIDATE_REGRESSES
    assert comparison.regressions


@pytest.mark.parametrize("workflow_id", WORKFLOW_IDS)
async def test_each_workflow_emits_allowlisted_events_and_retries_faults(
    workflow_id: str,
) -> None:
    workflow = _workflow(workflow_id)
    run = await run_reference_case(
        workflow=workflow,
        plan=workflow.baseline_plan,
        side="baseline",
        label="baseline",
    )
    kinds = [event.kind.value for event in run.events]
    assert "environment.created" in kinds
    assert "tool.selected" in kinds
    assert "dependency.result" in kinds
    assert "run.completed" in kinds
    if workflow.fault_script is not None and getattr(workflow.fault_script, "entries", ()):
        assert "fault.injected" in kinds
        assert run.retries >= 1, f"{workflow_id} should retry the injected fault"


def test_onboarding_live_copy_matches_the_german_workflow() -> None:
    """The Berlin fixture must not describe U.S.-specific identity forms."""
    workflow = _workflow("onboarding")
    position = next(tool for tool in workflow.tools if tool.name == "get_position")
    checklist = next(tool for tool in workflow.tools if tool.name == "select_checklist")

    position_text = position.run(workflow.repository, {})
    checklist_text = checklist.run(workflow.repository, {"source": "specific"})

    assert "German right-to-work" in position_text
    assert "right-to-work" in checklist_text
    assert "E-Verify" not in position_text + checklist_text
    assert "I-9" not in position_text + checklist_text


async def test_flight_booking_confirms_the_real_held_pnr() -> None:
    """The confirm step must use the PNR the hold step actually produced."""
    workflow = _workflow("flight_booking")
    run = await run_reference_case(
        workflow=workflow,
        plan=workflow.baseline_plan,
        side="baseline",
        label="baseline",
    )

    bookings = run.final_state.get("bookings", [])
    assert bookings, "flight booking left no booking in the final state"
    confirmed = [b for b in bookings if b.get("status") == "confirmed"]
    assert confirmed, "flight booking claims confirmation without confirmed state"
    pnr = confirmed[0]["pnr"]
    assert pnr, "confirmed booking has no PNR"
    assert pnr == run.business_metrics["pnr"]
    assert "PLACEHOLDER" not in pnr
    assert "booking:held->confirmed" in run.transitions
    assert run.verdict == "reproduced"


async def test_fault_retry_reruns_the_same_call_and_succeeds() -> None:
    """A one-shot fault must be retried as the SAME tool call, in place."""
    workflow = _workflow("flight_booking")
    run = await run_reference_case(
        workflow=workflow,
        plan=workflow.baseline_plan,
        side="baseline",
        label="baseline",
    )

    calls = list(run.tool_calls)
    assert calls.count("get_fare") == 2, f"get_fare was not retried in place: {calls}"
    assert calls[1] == calls[2] == "get_fare"
    assert run.retries == 1
    assert run.verdict == "reproduced"


def _two_fare_calls_plan(second_arguments: dict[str, object]) -> ReferencePlan:
    """This function builds a plan that calls get_fare twice, then holds."""
    passenger_id = _workflow("flight_booking").seed_state.passengers[0].id
    flight_id = _workflow("flight_booking").seed_state.flights[0].id
    return ReferencePlan(
        routing={"intent": "book_flight"},
        tool_calls=(
            ReferenceToolCall(tool="get_fare", arguments={"flight_id": str(flight_id)}),
            ReferenceToolCall(tool="get_fare", arguments=second_arguments),
            ReferenceToolCall(
                tool="hold_booking",
                arguments={"passenger_id": str(passenger_id), "flight_id": str(flight_id)},
            ),
            ReferenceToolCall(
                tool="confirm_booking",
                arguments={"pnr": "$hold_booking.pnr", "customer_token": "approve-7f3a-c91d"},
            ),
        ),
        gate_verified=True,
    )


async def test_retry_budget_is_per_call_when_tool_is_called_twice() -> None:
    """One successful use of a tool must not eat the next call's retries.

    The second get_fare call faults twice (repeat fault scoped by its
    arguments); it must get its own full retry budget, and a successful
    first call must not shorten it.
    """
    workflow = _workflow("flight_booking")
    second_args = {"flight_id": "second-flight"}
    workflow = dataclasses.replace(
        workflow,
        fault_script=FaultScript(
            script_version="1",
            dependency="fare.service",
            entries=(
                FaultScriptEntry(
                    kind=FaultKind.TIMEOUT,
                    tool="get_fare",
                    arguments=second_args,
                    repeat=True,
                ),
            ),
        ),
    )
    run = await run_reference_case(
        workflow=workflow,
        plan=_two_fare_calls_plan(second_args),
        side="baseline",
        label="baseline",
    )

    assert run.verdict == "failed"
    assert any("retries_exhausted" in error for error in run.errors)
    assert run.retries == MAX_RETRIES_PER_CALL
    assert run.tool_calls.count("get_fare") == 1 + MAX_RETRIES_PER_CALL + 1
    assert run.cleanup_ok is True


async def test_second_call_fault_retries_in_place_and_succeeds() -> None:
    """A one-shot fault on the SECOND use of a tool is retried in place."""
    workflow = _workflow("flight_booking")
    second_args = {"flight_id": "second-flight"}
    workflow = dataclasses.replace(
        workflow,
        fault_script=FaultScript(
            script_version="1",
            dependency="fare.service",
            entries=(
                FaultScriptEntry(
                    kind=FaultKind.TIMEOUT,
                    tool="get_fare",
                    arguments=second_args,
                ),
            ),
        ),
    )
    run = await run_reference_case(
        workflow=workflow,
        plan=_two_fare_calls_plan(second_args),
        side="baseline",
        label="baseline",
    )

    calls = list(run.tool_calls)
    assert calls[1] == calls[2] == "get_fare", f"second call not retried in place: {calls}"
    assert run.retries == 1
    assert run.verdict == "reproduced"
    assert run.cleanup_ok is True


async def test_fault_retry_exhaustion_fails_with_evidence() -> None:
    """A repeating fault must fail the run after the retry limit."""
    workflow = _workflow("flight_booking")
    workflow = dataclasses.replace(
        workflow,
        fault_script=FaultScript(
            script_version="1",
            dependency="fare.service",
            entries=(
                FaultScriptEntry(kind=FaultKind.TIMEOUT, tool="get_fare", repeat=True),
            ),
        ),
    )
    run = await run_reference_case(
        workflow=workflow,
        plan=workflow.baseline_plan,
        side="baseline",
        label="baseline",
    )

    assert run.verdict == "failed"
    assert any("retries_exhausted" in error for error in run.errors)
    assert run.retries == MAX_RETRIES_PER_CALL
    assert run.tool_calls.count("get_fare") == MAX_RETRIES_PER_CALL + 1
    assert run.cleanup_ok is True


async def test_disallowed_transition_fails_with_evidence() -> None:
    """An observed transition outside the permitted set must fail the run."""
    workflow = _workflow("flight_booking")
    workflow = dataclasses.replace(
        workflow,
        expectation=ReferenceExpectation(
            outcome="completed",
            reason_codes=("booking_confirmed",),
            permitted_transitions=(),
            required_transitions=(),
            gate_required=True,
            gate_tool="confirm_booking",
            protected_tools=("confirm_booking",),
        ),
    )
    run = await run_reference_case(
        workflow=workflow,
        plan=workflow.baseline_plan,
        side="baseline",
        label="baseline",
    )

    assert run.verdict == "failed"
    assert run.reason_code == "transition_contract_violation"
    assert any("disallowed state transition" in error for error in run.errors)


async def test_missing_required_transition_fails_with_evidence() -> None:
    """A required transition that never happened must fail the run."""
    workflow = _workflow("flight_booking")
    workflow = dataclasses.replace(
        workflow,
        expectation=ReferenceExpectation(
            outcome="completed",
            reason_codes=("booking_confirmed",),
            permitted_transitions=("booking:created", "booking:held->confirmed"),
            required_transitions=("booking:created", "page:created"),
            gate_required=True,
            gate_tool="confirm_booking",
            protected_tools=("confirm_booking",),
        ),
    )
    run = await run_reference_case(
        workflow=workflow,
        plan=workflow.baseline_plan,
        side="baseline",
        label="baseline",
    )

    assert run.verdict == "failed"
    assert run.reason_code == "transition_contract_violation"
    assert any("missing required state transition" in error for error in run.errors)


async def test_flight_booking_gate_blocks_confirmation_without_approval() -> None:
    workflow = _workflow("flight_booking")
    run = await run_reference_case(
        workflow=workflow,
        plan=workflow.candidate_plan,
        side="candidate",
        label="auto-confirm",
    )
    assert run.safety_ok is False
    assert any("approval gate" in error for error in run.errors)
    assert run.cleanup_ok is True


async def test_flight_booking_replay_has_deterministic_business_state() -> None:
    workflow = _workflow("flight_booking")

    first = await run_reference_case(
        workflow=workflow,
        plan=workflow.baseline_plan,
        side="baseline",
        label="first",
    )
    second = await run_reference_case(
        workflow=workflow,
        plan=workflow.baseline_plan,
        side="baseline",
        label="second",
    )

    assert first.final_state_hash == second.final_state_hash
    assert first.mutations == second.mutations


async def test_unknown_tool_call_fails_clearly() -> None:
    workflow = _workflow("flight_booking")
    plan = ReferencePlan(
        routing={"intent": "book_flight"},
        tool_calls=(ReferenceToolCall(tool="not_a_tool", arguments={}),),
    )
    run = await run_reference_case(
        workflow=workflow,
        plan=plan,
        side="baseline",
        label="broken",
    )
    assert run.verdict == "failed"
    assert any("unknown tool" in error for error in run.errors)
    assert run.cleanup_ok is True


class _FailingDestroyRepository(InMemoryReferenceRepository):
    """This class destroys with an error to test cleanup failure handling."""

    def destroy(self) -> None:
        raise RuntimeError("destroy exploded")


async def test_incident_mutation_before_values_reflect_prior_state() -> None:
    """The ack->remediate path must record acknowledged->mitigated, not a lie."""
    workflow = _workflow("incident_response")
    run = await run_reference_case(
        workflow=workflow,
        plan=workflow.baseline_plan,
        side="baseline",
        label="baseline",
    )

    status_mutations = [
        mutation
        for mutation in run.mutations
        if mutation.get("field") == "status" and mutation.get("resource") == "incident"
    ]
    assert [(m["before"], m["after"]) for m in status_mutations] == [
        ("triggered", "acknowledged"),
        ("acknowledged", "mitigated"),
    ]


@pytest.mark.parametrize("workflow_id", WORKFLOW_IDS)
async def test_each_mutation_before_value_matches_the_state_before_the_call(
    workflow_id: str,
) -> None:
    """For every workflow, status transitions must chain: each before equals
    the previous after on the same resource."""
    workflow = _workflow(workflow_id)
    run = await run_reference_case(
        workflow=workflow,
        plan=workflow.baseline_plan,
        side="baseline",
        label="baseline",
    )
    by_resource: dict[str, list[dict[str, object]]] = {}
    for mutation in run.mutations:
        if mutation.get("field") == "status":
            by_resource.setdefault(str(mutation.get("resource")), []).append(mutation)
    for resource, mutations in by_resource.items():
        for previous, current in zip(mutations, mutations[1:]):
            assert current["before"] == previous["after"], (
                f"{workflow_id} {resource}: mutation before value {current['before']!r} "
                f"does not match the prior state {previous['after']!r}"
            )


async def test_cleanup_failure_is_recorded_and_blocks_positive_verdict() -> None:
    workflow = dataclasses.replace(
        _workflow("flight_booking"),
        repository=_FailingDestroyRepository(),
    )
    run = await run_reference_case(
        workflow=workflow,
        plan=workflow.baseline_plan,
        side="baseline",
        label="baseline",
    )

    assert run.cleanup_ok is False
    assert run.verdict == "failed"
    assert run.reason_code == "cleanup_failed"
    assert any("cleanup failed" in error for error in run.errors)
    # The primary run result stays visible even though cleanup failed.
    assert run.final_state
    assert run.mutations
    assert run.business_outcome


async def test_cleanup_success_emits_destroyed_event() -> None:
    workflow = _workflow("flight_booking")
    run = await run_reference_case(
        workflow=workflow,
        plan=workflow.baseline_plan,
        side="baseline",
        label="baseline",
    )
    kinds = [event.kind.value for event in run.events]
    assert "environment.destroyed" in kinds
    assert run.cleanup_ok is True


async def test_repository_destroy_leaves_no_state_behind() -> None:
    workflow = _workflow("flight_booking")
    run = await run_reference_case(
        workflow=workflow,
        plan=workflow.baseline_plan,
        side="baseline",
        label="baseline",
    )
    assert run.cleanup_ok is True
    assert run.final_state_hash
    assert run.mutations
    assert workflow.repository._state is None  # type: ignore[attr-defined]
