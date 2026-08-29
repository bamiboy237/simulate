"""This module implements the baseline/candidate suite comparison.

A suite comparison runs every exact saved-case version twice: once with the
deployed baseline configuration and once with exactly one declared candidate
change. The existing one-variable experiment contract rejects hidden
multi-variable changes before any run starts, and the existing per-case
comparison produces the verdicts. Any regression keeps the baseline, any
incomplete evidence makes the cohort inconclusive, and only then does task
success decide the recommendation.
"""

from collections.abc import Sequence
from typing import Literal, cast

from app.adapters.pydantic_ai_agent import ModelConfig
from app.domain.bundle.schemas import ConfigurationVersions, SimulationBundle
from app.domain.comparison.compare import ComparisonVerdict, compare_runs
from app.domain.comparison.evaluators import (
    ALL_EVALUATORS,
    SAFETY_EVALUATORS,
    Evaluator,
)
from app.domain.comparison.experiment import (
    ConfigurationChangeType,
    ConfigurationSet,
    validate_baseline_matches_bundle,
)
from app.domain.regression.schemas import RegressionCase
from app.domain.simulation.provisioner import ProvisionerFactory
from app.domain.simulation.runner import RunVerdict, SimulationRun, run_bundle
from app.domain.suite.errors import InvalidSuiteError, SuiteRunError, UnsupportedChangeError
from app.domain.suite.schemas import (
    CaseSuite,
    SuiteCaseResult,
    SuiteComparisonResult,
    SuiteComparisonTotals,
    SuiteSideTotals,
    SuiteVerdict,
)

EXECUTABLE_CHANGE_TYPES = frozenset(
    {ConfigurationChangeType.MODEL, ConfigurationChangeType.PROMPT}
)


def _task_success(run: SimulationRun, bundle: SimulationBundle) -> bool:
    """This function decides whether one run completed the expected task safely.

    A task counts as successful only when the run produced the expected
    business outcome and every required safety and state evaluator passed.
    """
    if run.response is None or run.response.outcome is not bundle.expected_behavior.outcome:
        return False
    present = {result.evaluator: result.passed for result in run.evaluators.results}
    return all(name in present and present[name] for name in SAFETY_EVALUATORS)


def _validate_cases(suite: CaseSuite, cases: Sequence[RegressionCase]) -> None:
    """This function rejects a case set that does not match the suite members."""
    if len(cases) != len(suite.members):
        raise InvalidSuiteError(
            detail=f"resolved {len(cases)} cases for {len(suite.members)} suite members"
        )
    for member, case in zip(suite.members, cases):
        if member.case_id != case.case_id or member.case_version != case.case_version:
            raise InvalidSuiteError(
                detail=(
                    f"resolved case {case.case_id!r} v{case.case_version} does not "
                    f"match member {member.case_id!r} v{member.case_version}"
                )
            )


def _validate_experiment(
    case: RegressionCase,
    change_type: ConfigurationChangeType,
    candidate: ConfigurationVersions,
) -> None:
    """This function rejects hidden changes, multiple changes, and baseline edits."""
    bundle = case.bundle
    if bundle.bundle_id is None:
        raise InvalidSuiteError(detail=f"case {case.case_id!r} bundle has no derived id")
    try:
        experiment = ConfigurationSet(
            bundle_id=bundle.bundle_id,
            change_type=change_type,
            baseline=bundle.configuration_versions,
            candidate=candidate,
        )
        validate_baseline_matches_bundle(experiment, bundle)
    except ValueError as error:
        # The one-variable validator raises ExperimentError, which pydantic
        # wraps in ValidationError during model construction. Both are
        # ValueErrors; the suite boundary reports one safe typed error.
        raise SuiteRunError(
            detail=f"case {case.case_id!r} v{case.case_version}: {error}"
        ) from error


def _validate_model_baseline(case: RegressionCase, baseline: ModelConfig) -> None:
    """Reject a runtime baseline that differs from the model recorded on the case."""
    recorded = case.bundle.configuration_versions
    if baseline.provider != recorded.model_provider or baseline.name != recorded.model_name:
        raise SuiteRunError(
            detail=(
                f"baseline model {baseline.provider!r}/{baseline.name!r} does not match the model "
                f"recorded on case {case.case_id!r} v{case.case_version}"
            )
        )


def _side_totals(
    runs: Sequence[SimulationRun],
    success_flags: Sequence[bool],
) -> SuiteSideTotals:
    """This function aggregates one side's measured totals from its runs."""
    totals = SuiteSideTotals()
    for run, success in zip(runs, success_flags):
        if success:
            totals.success_count += 1
        totals.total_latency_ms += run.total_latency_ms or 0.0
        totals.total_tokens += run.total_tokens if run.total_tokens > 0 else 0
        totals.total_cost_usd += run.cost_usd or 0.0
        totals.total_retries += run.retries
    return totals


async def run_suite_comparison(
    *,
    suite: CaseSuite,
    cases: Sequence[RegressionCase],
    change_type: ConfigurationChangeType,
    candidate: ConfigurationVersions,
    provisioner_factory: ProvisionerFactory,
    baseline_model_config: ModelConfig,
    candidate_prompt: str | None = None,
    candidate_prompt_version: str | None = None,
    evaluators: Sequence[Evaluator] = ALL_EVALUATORS,
) -> SuiteComparisonResult:
    """This function compares baseline and one candidate across one suite.

    Both sides start from the exact same approved bundle state. The declared
    change dimension is validated against every case before any run starts;
    the reference runner executes model and prompt changes and rejects the
    other dimensions with a clear typed error.
    """
    _validate_cases(suite, cases)
    if change_type not in EXECUTABLE_CHANGE_TYPES:
        raise UnsupportedChangeError(change_type=change_type.value)
    for case in cases:
        _validate_experiment(case, change_type, candidate)
        if change_type is ConfigurationChangeType.MODEL:
            _validate_model_baseline(case, baseline_model_config)

    case_results, baseline_runs, candidate_runs, baseline_successes, candidate_successes = (
        await _run_and_compare_cases(
            cases,
            change_type=change_type,
            candidate=candidate,
            provisioner_factory=provisioner_factory,
            baseline_model_config=baseline_model_config,
            candidate_prompt=candidate_prompt,
            candidate_prompt_version=candidate_prompt_version,
            evaluators=evaluators,
        )
    )
    return _finalize_result(
        suite=suite,
        change_type=change_type,
        case_results=case_results,
        baseline_runs=baseline_runs,
        candidate_runs=candidate_runs,
        baseline_successes=baseline_successes,
        candidate_successes=candidate_successes,
        baseline_label=f"{cases[0].bundle.configuration_versions.model_provider or 'unknown'}/"
        f"{cases[0].bundle.configuration_versions.model_name or 'unknown'}"
        if change_type is ConfigurationChangeType.MODEL
        else (cases[0].bundle.configuration_versions.answer_instructions_version or "baseline"),
        candidate_label=(
            f"{candidate.model_provider or 'unknown'}/{candidate.model_name or 'unknown'}"
            if change_type is ConfigurationChangeType.MODEL
            else (
                candidate_prompt_version
                or candidate.answer_instructions_version
                or "candidate"
            )
        ),
    )


async def run_cohort_model_comparison(
    *,
    suite: CaseSuite,
    cases: Sequence[RegressionCase],
    provisioner_factory: ProvisionerFactory,
    baseline_model_config: ModelConfig,
    candidate_model_config: ModelConfig,
    evaluators: Sequence[Evaluator] = ALL_EVALUATORS,
) -> SuiteComparisonResult:
    """This function compares two models across one heterogeneous cohort.

    The eight reference scenarios carry different recorded configurations
    (for example different policy versions), so the strict one-variable
    suite contract cannot hold for one shared candidate. This cohort
    comparison therefore changes only the model for every case, validates
    that the baseline model matches each bundle's recorded model, and runs
    every case with both model configurations from the exact same approved
    state. The per-case verdicts and the cohort totals follow the same
    deterministic rules as the suite comparison.
    """
    _validate_cases(suite, cases)
    for case in cases:
        _validate_model_baseline(case, baseline_model_config)

    case_results, baseline_runs, candidate_runs, baseline_successes, candidate_successes = (
        await _run_and_compare_cases(
            cases,
            change_type=ConfigurationChangeType.MODEL,
            candidate=None,
            provisioner_factory=provisioner_factory,
            baseline_model_config=baseline_model_config,
            candidate_model_config=candidate_model_config,
            evaluators=evaluators,
        )
    )
    return _finalize_result(
        suite=suite,
        change_type=ConfigurationChangeType.MODEL,
        case_results=case_results,
        baseline_runs=baseline_runs,
        candidate_runs=candidate_runs,
        baseline_successes=baseline_successes,
        candidate_successes=candidate_successes,
        baseline_label=f"{baseline_model_config.provider}/{baseline_model_config.name}",
        candidate_label=f"{candidate_model_config.provider}/{candidate_model_config.name}",
    )


async def _run_and_compare_cases(
    cases: Sequence[RegressionCase],
    *,
    change_type: ConfigurationChangeType,
    candidate: ConfigurationVersions | None,
    provisioner_factory: ProvisionerFactory,
    baseline_model_config: ModelConfig,
    candidate_prompt: str | None = None,
    candidate_prompt_version: str | None = None,
    candidate_model_config: ModelConfig | None = None,
    evaluators: Sequence[Evaluator] = ALL_EVALUATORS,
) -> tuple[
    list[SuiteCaseResult],
    list[SimulationRun],
    list[SimulationRun],
    list[bool],
    list[bool],
]:
    """This function runs and compares every case and returns the raw cohort data."""
    case_results: list[SuiteCaseResult] = []
    baseline_runs: list[SimulationRun] = []
    candidate_runs: list[SimulationRun] = []
    baseline_successes: list[bool] = []
    candidate_successes: list[bool] = []

    for case in cases:
        bundle = case.bundle
        baseline_run = await run_bundle(
            bundle=bundle,
            provisioner_factory=provisioner_factory,
            model_config=baseline_model_config,
            evaluators=evaluators,
        )
        if change_type is ConfigurationChangeType.MODEL:
            if candidate_model_config is not None:
                candidate_run = await run_bundle(
                    bundle=bundle,
                    provisioner_factory=provisioner_factory,
                    model_config=candidate_model_config,
                    evaluators=evaluators,
                )
            else:
                assert candidate is not None
                candidate_provider = candidate.model_provider
                candidate_name = candidate.model_name
                if candidate_provider is None or candidate_name is None:
                    raise SuiteRunError(
                        detail="a model change requires provider and name on the candidate"
                    )
                candidate_run = await run_bundle(
                    bundle=bundle,
                    provisioner_factory=provisioner_factory,
                    model_config=ModelConfig(
                        provider=cast("Literal['openai', 'anthropic']", candidate_provider),
                        name=candidate_name,
                    ),
                    evaluators=evaluators,
                )
        else:
            assert candidate is not None
            if candidate_prompt is None:
                raise SuiteRunError(detail="a prompt change requires candidate prompt text")
            candidate_run = await run_bundle(
                bundle=bundle,
                provisioner_factory=provisioner_factory,
                model_config=baseline_model_config,
                answer_instructions=candidate_prompt,
                answer_instructions_version=candidate_prompt_version
                or candidate.answer_instructions_version,
                evaluators=evaluators,
            )

        comparison = compare_runs(baseline_run, candidate_run, bundle)
        case_results.append(
            SuiteCaseResult(
                case_id=case.case_id,
                case_version=case.case_version,
                scenario_id=case.scenario_id,
                bundle_content_hash=case.bundle_content_hash,
                evidence_ref=case.evidence_ref,
                evidence_content_hash=case.evidence_content_hash,
                comparison=comparison,
            )
        )
        baseline_runs.append(baseline_run)
        candidate_runs.append(candidate_run)
        baseline_successes.append(_task_success(baseline_run, bundle))
        candidate_successes.append(_task_success(candidate_run, bundle))
    return case_results, baseline_runs, candidate_runs, baseline_successes, candidate_successes


def _finalize_result(
    *,
    suite: CaseSuite,
    change_type: ConfigurationChangeType,
    case_results: list[SuiteCaseResult],
    baseline_runs: list[SimulationRun],
    candidate_runs: list[SimulationRun],
    baseline_successes: list[bool],
    candidate_successes: list[bool],
    baseline_label: str,
    candidate_label: str,
) -> SuiteComparisonResult:
    """This function aggregates one cohort into the deterministic result."""
    counts = {
        "passes": 0,
        "regressions": 0,
        "no_difference": 0,
        "insufficient": 0,
        "safety_regressions": 0,
        "missing_measurement_cases": 0,
        "coverage_failure_cases": 0,
    }
    for case_result in case_results:
        comparison = case_result.comparison
        if comparison.verdict is ComparisonVerdict.CANDIDATE_PASSES:
            counts["passes"] += 1
        elif comparison.verdict is ComparisonVerdict.CANDIDATE_REGRESSES:
            counts["regressions"] += 1
        elif comparison.verdict is ComparisonVerdict.NO_MATERIAL_DIFFERENCE:
            counts["no_difference"] += 1
        else:
            counts["insufficient"] += 1
        missing_in_case = any(
            "missing required measurement" in reason
            for reason in comparison.blocked_reasons
        )
        if missing_in_case:
            counts["missing_measurement_cases"] += 1
        if (
            comparison.baseline_run.verdict is RunVerdict.MISSING_COVERAGE
            or comparison.candidate_run.verdict is RunVerdict.MISSING_COVERAGE
        ):
            counts["coverage_failure_cases"] += 1
        safety_in_case = any(
            "safety regression" in reason for reason in comparison.blocked_reasons
        )
        if safety_in_case:
            counts["safety_regressions"] += 1

    baseline_totals = _side_totals(baseline_runs, baseline_successes)
    candidate_totals = _side_totals(candidate_runs, candidate_successes)
    totals = SuiteComparisonTotals(
        cases=len(case_results),
        comparable=len(case_results) - counts["insufficient"],
        candidate_passes=counts["passes"],
        candidate_regresses=counts["regressions"],
        no_material_difference=counts["no_difference"],
        insufficient_evidence=counts["insufficient"],
        safety_regressions=counts["safety_regressions"],
        missing_measurement_cases=counts["missing_measurement_cases"],
        coverage_failure_cases=counts["coverage_failure_cases"],
        baseline=baseline_totals,
        candidate=candidate_totals,
    )

    if counts["regressions"] > 0 or counts["safety_regressions"] > 0:
        verdict = SuiteVerdict.KEEP_BASELINE
        reason = (
            f"the candidate regressed {counts['regressions']} case(s) "
            f"({counts['safety_regressions']} safety regression(s)); the baseline stays"
        )
    elif counts["insufficient"] > 0:
        verdict = SuiteVerdict.INCONCLUSIVE
        reason = (
            f"{counts['insufficient']} case(s) lack comparable evidence "
            f"({counts['missing_measurement_cases']} missing measurement, "
            f"{counts['coverage_failure_cases']} coverage failure); no recommendation"
        )
    elif candidate_totals.success_count > baseline_totals.success_count:
        verdict = SuiteVerdict.RECOMMEND_CANDIDATE
        reason = (
            f"the candidate completes {candidate_totals.success_count} task(s) versus "
            f"{baseline_totals.success_count} for the baseline"
        )
    elif candidate_totals.success_count < baseline_totals.success_count:
        verdict = SuiteVerdict.KEEP_BASELINE
        reason = (
            f"the candidate completes {candidate_totals.success_count} task(s) versus "
            f"{baseline_totals.success_count} for the baseline; the baseline stays"
        )
    else:
        verdict = SuiteVerdict.INCONCLUSIVE
        reason = (
            "baseline and candidate complete the same number of tasks and no "
            "case regressed; the cohort evidence is not decisive"
        )

    return SuiteComparisonResult(
        suite_id=suite.suite_id,
        suite_version=suite.suite_version,
        suite_name=suite.name,
        change_type=change_type,
        baseline_label=baseline_label,
        candidate_label=candidate_label,
        cases=tuple(case_results),
        totals=totals,
        verdict=verdict,
        verdict_reason=reason,
    )
