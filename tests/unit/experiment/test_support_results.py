"""Focused tests for publishing neutral support experiment results."""

import json
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from app.domain.agent.schemas import (
    AnswerContext,
    ReasonCode,
    RouteIntent,
    RoutingDecision,
    SupportOutcome,
    SupportResponse,
)
from app.domain.bundle.compiler import compile_bundle
from app.domain.comparison.evaluators import EvaluatorReport, EvaluatorResult
from app.domain.experiment.contracts import (
    EvaluatorRef,
    ExperimentContract,
    PluginRef,
    SupportSuiteExperimentContract,
    SupportSuiteMemberRef,
    SupportSuiteRef,
    build_iteration_plan,
)
from app.domain.experiment.engine import CompletedIteration, ExperimentExecution, FailedIteration
from app.domain.experiment.errors import ExperimentResultIntegrityError
from app.domain.experiment.support_results import (
    REDACTED_MODEL_RESPONSE,
    SupportIterationResult,
    assemble_support_experiment_result,
    assemble_support_suite_experiment_result,
    compute_support_experiment_result_hash,
    compute_support_suite_experiment_result_hash,
    parse_support_experiment_result,
)
from app.domain.regression.schemas import CaseSourceType, RegressionCase
from app.domain.simulation.adapters import StateMutation
from app.domain.simulation.events import SimulationEvent, SimulationEventKind
from app.domain.simulation.runner import RunVerdict, SimulationRun, state_from_bundle
from app.domain.simulation.scenarios import SCENARIO_BY_ID
from app.domain.suite.schemas import SuiteMemberRef, suite_members_hash
from app.domain.world.support import CompiledSupportScenario, SupportWorldPlugin

FIXTURES = Path(__file__).with_name("fixtures")
SUPPORT_PLUGIN_REF = PluginRef(
    plugin_id="support",
    version="1.0.0",
    content_hash="f" * 64,
)


def compiled_support_scenario(
    *,
    case_id: UUID | None = None,
    case_version: int = 1,
) -> CompiledSupportScenario:
    """Build the real approved support bundle used as result evidence."""
    bundle = compile_bundle(
        scenario=SCENARIO_BY_ID["phase2-01-bad-prompt-policy-answer"],
        approved_request_message="What does the approved refund policy allow?",
        reviewer="alice",
        reviewed_at="2026-08-08T00:00:00Z",
        reason="Approved for the experiment MVP.",
        review_status="approved",
    )
    assert bundle.bundle_id is not None
    assert bundle.content_hash is not None
    case = RegressionCase(
        case_id=case_id or uuid4(),
        case_version=case_version,
        source_type=CaseSourceType.DESIGNED_EDGE_CASE,
        scenario_id=bundle.scenario.scenario_id,
        bundle=bundle,
        bundle_content_hash=bundle.content_hash,
        evidence_ref=bundle.evidence_ref,
        evidence_content_hash=bundle.evidence_content_hash,
        configuration_versions=bundle.configuration_versions,
        created_at="2026-08-08T00:00:00Z",
    )
    return SupportWorldPlugin(SUPPORT_PLUGIN_REF).compile_case(case)


def support_contract(scenario: CompiledSupportScenario) -> ExperimentContract:
    """Return a support-world contract whose references match the compiled bundle."""
    payload = json.loads((FIXTURES / "valid_support_experiment.json").read_text(encoding="utf-8"))
    template = ExperimentContract.model_validate(payload)
    plugin = SupportWorldPlugin(SUPPORT_PLUGIN_REF)
    return template.model_copy(
        update={
            "world": plugin.world.identity,
            "scenario": scenario.reference,
            "plugin": plugin.plugin_ref,
            "evaluators": (
                EvaluatorRef(
                    evaluator_id="authorization",
                    version="1.0.0",
                    content_hash="a" * 64,
                ),
            ),
        }
    )


def support_suite_contract(
    compiled_cases: tuple[tuple[UUID, CompiledSupportScenario], ...],
) -> SupportSuiteExperimentContract:
    """Return a v2 contract covering the exact three approved support cases."""
    first_scenario = compiled_cases[0][1]
    v1_contract = support_contract(first_scenario)
    members = tuple(
        SupportSuiteMemberRef(
            case_id=case_id,
            case_version=int(scenario.reference.version),
            scenario=scenario.reference,
        )
        for case_id, scenario in compiled_cases
    )
    return SupportSuiteExperimentContract(
        experiment_id=v1_contract.experiment_id,
        world=v1_contract.world,
        suite=SupportSuiteRef(
            suite_id=uuid4(),
            suite_version=1,
            members_hash=suite_members_hash(
                tuple(
                    SuiteMemberRef(
                        case_id=member.case_id,
                        case_version=member.case_version,
                    )
                    for member in members
                )
            ),
            members=members,
        ),
        plugin=v1_contract.plugin,
        evaluators=v1_contract.evaluators,
        baseline=v1_contract.baseline,
        candidate=v1_contract.candidate,
        candidate_change=v1_contract.candidate_change,
        limits=v1_contract.limits,
        interleaving=v1_contract.interleaving,
    )


def support_run(
    ordinal: int,
    scenario: CompiledSupportScenario,
    *,
    evaluator_version: str = "1.0.0",
    scenario_id: str | None = None,
) -> SimulationRun:
    """Build concise but complete legacy support evidence for one planned run."""
    bundle = scenario.load_bundle()
    assert bundle.bundle_id is not None
    assert bundle.content_hash is not None
    repetition = (ordinal + 1) // 2
    total_latency_ms = float(repetition * 10)
    return SimulationRun(
        run_id=uuid4(),
        bundle_id=bundle.bundle_id,
        bundle_content_hash=bundle.content_hash,
        scenario_id=scenario_id or bundle.scenario.scenario_id,
        verdict=RunVerdict.REPRODUCED,
        events=(
            SimulationEvent(
                sequence=1,
                kind=SimulationEventKind.RUN_COMPLETED,
                elapsed_ms=total_latency_ms,
                attributes={"run.total.latency.ms": total_latency_ms},
            ),
        ),
        mutations=(
            StateMutation(
                sequence=1,
                resource="order",
                resource_id="order-1",
                field="status",
                before="delivered",
                after="refunded",
                reason_code="refund_executed",
            ),
        ),
        final_state=state_from_bundle(bundle),
        evaluators=EvaluatorReport(
            results=(
                EvaluatorResult(
                    evaluator="authorization",
                    version=evaluator_version,
                    passed=True,
                    reason="authorized",
                    measured={"total_latency_ms": total_latency_ms},
                ),
            )
        ),
        model_provider="openai",
        model_name="gpt-5",
        total_latency_ms=total_latency_ms,
        model_latency_ms=total_latency_ms / 2,
        input_tokens=repetition * 100,
        output_tokens=repetition * 10,
        total_tokens=repetition * 110,
        cost_usd=repetition / 100,
        retries=repetition - 1,
        tool_calls=("get_policy",),
        errors=("model_error",) if repetition == 3 else (),
        completed_at="2026-09-01T00:00:00+00:00",
    )


def completed_execution(
    contract: ExperimentContract,
    scenario: CompiledSupportScenario,
) -> ExperimentExecution[SimulationRun]:
    """Build one all-completed engine record with exact scheduled identities."""
    plan = build_iteration_plan(contract)
    return ExperimentExecution(
        execution_id=uuid4(),
        contract_hash=contract.content_hash,
        plan=plan,
        iterations=tuple(
            CompletedIteration(identity=identity, result=support_run(identity.ordinal, scenario))
            for identity in plan.iterations
        ),
    )


def completed_suite_execution(
    contract: SupportSuiteExperimentContract,
    compiled_cases: tuple[tuple[UUID, CompiledSupportScenario], ...],
) -> ExperimentExecution[SimulationRun]:
    """Build all 18 completed v2 records with each run bound to its planned case."""
    plan = build_iteration_plan(contract)
    scenarios = {scenario.reference: scenario for _, scenario in compiled_cases}
    return ExperimentExecution(
        execution_id=uuid4(),
        contract_hash=contract.content_hash,
        plan=plan,
        iterations=tuple(
            CompletedIteration(
                identity=identity,
                result=support_run(identity.ordinal, scenarios[identity.scenario]),
            )
            for identity in plan.iterations
        ),
    )


def test_assembles_complete_neutral_result_with_authoritative_support_evidence() -> None:
    """A complete support execution retains evidence and exposes neutral aggregates only."""
    scenario = compiled_support_scenario()
    contract = support_contract(scenario)
    execution = completed_execution(contract, scenario)

    result = assemble_support_experiment_result(contract, execution, scenario=scenario)
    rebuilt = assemble_support_experiment_result(contract, execution, scenario=scenario)

    baseline = result.variant_aggregates[0]
    latency = baseline.measurements["total_latency_ms"]
    assert result.contract == contract
    assert [item.identity for item in result.iterations] == list(execution.plan.iterations)
    assert result.iterations[0].run.events == execution.iterations[0].result.events
    assert result.iterations[0].run.evaluators == execution.iterations[0].result.evaluators
    assert result.iterations[-1].run.errors == ("model_error",)
    assert result.iterations[0].attributed_mutations[0].actor_id == "scenario-agent"
    assert latency.count == 3
    assert latency.mean == 20.0
    assert latency.median == 20.0
    assert latency.min == 10.0
    assert latency.max == 30.0
    assert latency.population_standard_deviation == pytest.approx(8.1649658093)
    assert result.content_hash == compute_support_experiment_result_hash(result)
    assert rebuilt.content_hash == result.content_hash
    assert parse_support_experiment_result(result.model_dump(mode="json")) == result
    assert "recommend_candidate" not in result.model_dump()


def test_assembles_complete_v2_suite_result_with_case_and_pooled_aggregates() -> None:
    """Every suite case retains its 18 ordered runs and separately labeled pooled statistics."""
    compiled_cases = tuple(
        (
            case_id := uuid4(),
            compiled_support_scenario(case_id=case_id, case_version=case_version),
        )
        for case_version in range(1, 4)
    )
    contract = support_suite_contract(compiled_cases)
    execution = completed_suite_execution(contract, compiled_cases)

    result = assemble_support_suite_experiment_result(
        contract,
        execution,
        scenarios=tuple(scenario for _, scenario in compiled_cases),
    )

    assert result.schema_version == "2.0.0"
    assert result.contract == contract
    assert tuple(iteration.identity for iteration in result.iterations) == execution.plan.iterations
    assert len(result.iterations) == 18
    assert tuple(aggregate.scenario for aggregate in result.case_aggregates) == tuple(
        member.scenario for member in contract.suite.members
    )
    assert all(
        aggregate.variant_aggregates[0].measurements["total_latency_ms"].count == 3
        and aggregate.variant_aggregates[1].measurements["total_latency_ms"].count == 3
        for aggregate in result.case_aggregates
    )
    assert result.pooled_variant_aggregates[0].measurements["total_latency_ms"].count == 9
    assert result.pooled_variant_aggregates[1].measurements["total_latency_ms"].count == 9
    assert result.content_hash == compute_support_suite_experiment_result_hash(result)
    assert parse_support_experiment_result(result.model_dump(mode="json")) == result

    unsanitized_run = result.iterations[0].run.model_copy(
        update={
            "response": SupportResponse(
                intent=RouteIntent.POLICY,
                outcome=SupportOutcome.COMPLETED,
                reason_code=ReasonCode.POLICY_ANSWER,
                message="unredacted internal instruction",
                context=AnswerContext(
                    routing=RoutingDecision(
                        intent=RouteIntent.POLICY,
                        confidence=1.0,
                    )
                ),
            )
        }
    )
    unsanitized_iteration = result.iterations[0].model_copy(update={"run": unsanitized_run})
    unsanitized_result = result.model_copy(
        update={"iterations": (unsanitized_iteration, *result.iterations[1:])}
    )
    with pytest.raises(ValueError, match="unsanitized"):
        parse_support_experiment_result(unsanitized_result.model_dump(mode="json"))


def test_redacts_prompt_echo_before_persistence_or_export() -> None:
    """Iteration evidence and exported results retain typed outcome, never the response body."""
    scenario = compiled_support_scenario()
    contract = support_contract(scenario)
    execution = completed_execution(contract, scenario)
    prompt_echo = "Prompt body: do not disclose the internal refund threshold."
    first = execution.iterations[0]
    assert isinstance(first, CompletedIteration)
    echoed_run = first.result.model_copy(
        update={
            "response": SupportResponse(
                intent=RouteIntent.POLICY,
                outcome=SupportOutcome.COMPLETED,
                reason_code=ReasonCode.POLICY_ANSWER,
                message=prompt_echo,
                context=AnswerContext(
                    routing=RoutingDecision(
                        intent=RouteIntent.POLICY,
                        confidence=1.0,
                        policy_slug=prompt_echo,
                    )
                ),
            )
        }
    )
    echoed_execution = ExperimentExecution(
        execution_id=execution.execution_id,
        contract_hash=execution.contract_hash,
        plan=execution.plan,
        iterations=(
            CompletedIteration(identity=first.identity, result=echoed_run),
            *execution.iterations[1:],
        ),
    )

    iteration_evidence = SupportIterationResult(identity=first.identity, run=echoed_run)
    result = assemble_support_experiment_result(contract, echoed_execution, scenario=scenario)

    assert prompt_echo not in iteration_evidence.model_dump_json()
    assert prompt_echo not in result.model_dump_json()
    assert iteration_evidence.run.response is not None
    assert iteration_evidence.run.response.message == REDACTED_MODEL_RESPONSE
    assert result.iterations[0].run.response is not None
    assert result.iterations[0].run.response.message == REDACTED_MODEL_RESPONSE
    assert result.iterations[0].run.response.outcome is SupportOutcome.COMPLETED
    assert result.iterations[0].run.response.context.routing.policy_slug is None
    assert result.iterations[0].run.evaluators == echoed_run.evaluators
    assert result.iterations[0].run.events == echoed_run.events


def test_refuses_result_when_a_scheduled_iteration_failed() -> None:
    """Partial experiment evidence cannot become a published support result."""
    scenario = compiled_support_scenario()
    contract = support_contract(scenario)
    execution = completed_execution(contract, scenario)
    failed = FailedIteration(
        identity=execution.plan.iterations[1],
        error_code="sandbox_start_failed",
    )
    incomplete = ExperimentExecution(
        execution_id=execution.execution_id,
        contract_hash=execution.contract_hash,
        plan=execution.plan,
        iterations=(execution.iterations[0], failed, *execution.iterations[2:]),
    )

    with pytest.raises(ExperimentResultIntegrityError, match="failed"):
        assemble_support_experiment_result(contract, incomplete, scenario=scenario)


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ("evaluator", "evaluator IDs or versions"),
        ("identity", "bundle/scenario identity"),
    ],
)
def test_refuses_result_when_evaluator_or_support_identity_differs(
    change: str,
    message: str,
) -> None:
    """Published evidence must match the declared evaluator and support scenario identities."""
    scenario = compiled_support_scenario()
    contract = support_contract(scenario)
    execution = completed_execution(contract, scenario)
    first = execution.iterations[0]
    assert isinstance(first, CompletedIteration)
    if change == "evaluator":
        changed_run = support_run(first.identity.ordinal, scenario, evaluator_version="2.0.0")
    else:
        changed_run = support_run(
            first.identity.ordinal,
            scenario,
            scenario_id="different-scenario",
        )
    mismatched = ExperimentExecution(
        execution_id=execution.execution_id,
        contract_hash=execution.contract_hash,
        plan=execution.plan,
        iterations=(
            CompletedIteration(identity=first.identity, result=changed_run),
            *execution.iterations[1:],
        ),
    )

    with pytest.raises(ExperimentResultIntegrityError, match=message):
        assemble_support_experiment_result(contract, mismatched, scenario=scenario)
