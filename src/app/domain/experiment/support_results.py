"""Assemble complete, neutral results from legacy support simulation runs.

This adapter deliberately sits outside the generic experiment engine. It knows
how a support ``SimulationRun`` identifies its compiled bundle and how legacy
state mutations are attributed. Other world plugins should supply their own
result adapters rather than extending these support-specific types.
"""

from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Iterable, Mapping
from hashlib import sha256
from statistics import median, pstdev
from typing import TYPE_CHECKING, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.domain.agent.schemas import AnswerContext, RoutingDecision
from app.domain.comparison.evaluators import EvaluatorReport
from app.domain.experiment.contracts import (
    ConfigurationVariant,
    EvaluatorRef,
    ExperimentContract,
    IterationIdentity,
    IterationPlan,
    ScenarioRef,
    SupportSuiteExperimentContract,
    build_iteration_plan,
)
from app.domain.experiment.engine import CompletedIteration, ExperimentExecution
from app.domain.experiment.errors import ExperimentResultIntegrityError
from app.domain.simulation.adapters import StateMutation
from app.domain.simulation.runner import SimulationRun

if TYPE_CHECKING:
    from app.domain.world.support import CompiledSupportScenario


SUPPORT_EXPERIMENT_RESULT_SCHEMA_VERSION = "1.0.0"
SUPPORT_SUITE_EXPERIMENT_RESULT_SCHEMA_VERSION: Literal["2.0.0"] = "2.0.0"
SUPPORT_SCENARIO_AGENT_ID: Literal["scenario-agent"] = "scenario-agent"
REDACTED_MODEL_RESPONSE = "[model response redacted]"


class NumericMeasurementAggregate(BaseModel):
    """Neutral descriptive statistics for the available observations of one measurement."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    count: int = Field(ge=1)
    mean: float
    median: float
    min: float
    max: float
    population_standard_deviation: float = Field(ge=0)


class SupportMutationAttribution(BaseModel):
    """One legacy support state mutation explicitly attributed to the scenario agent."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    actor_id: Literal["scenario-agent"] = SUPPORT_SCENARIO_AGENT_ID
    mutation: StateMutation


class SupportIterationResult(BaseModel):
    """The complete authoritative support-run evidence for one planned iteration."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    identity: IterationIdentity
    run: SimulationRun
    attributed_mutations: tuple[SupportMutationAttribution, ...] = ()

    @model_validator(mode="after")
    def scrub_response_before_serialization(self) -> "SupportIterationResult":
        """Ensure this persistence shape cannot retain model-generated response text."""
        object.__setattr__(self, "run", scrub_model_response(self.run))
        return self


class SupportVariantAggregate(BaseModel):
    """Neutral aggregates for all completed iterations of one configuration."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    variant: ConfigurationVariant
    measurements: dict[str, NumericMeasurementAggregate] = Field(default_factory=dict)


class SupportSuiteCaseAggregate(BaseModel):
    """Per-case aggregates keyed by the exact immutable scenario reference."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    scenario: ScenarioRef
    variant_aggregates: tuple[SupportVariantAggregate, ...] = Field(
        min_length=2,
        max_length=2,
    )

    @model_validator(mode="after")
    def require_both_variants(self) -> "SupportSuiteCaseAggregate":
        """Keep the baseline and candidate measurements explicit and canonically ordered."""
        variants = tuple(aggregate.variant for aggregate in self.variant_aggregates)
        expected = (ConfigurationVariant.BASELINE, ConfigurationVariant.CANDIDATE)
        if variants != expected:
            raise ExperimentResultIntegrityError(
                "case aggregates must contain baseline then candidate measurements"
            )
        return self


class SupportExperimentResult(BaseModel):
    """A complete, neutral, hash-verified result for one support experiment."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: str = Field(
        default=SUPPORT_EXPERIMENT_RESULT_SCHEMA_VERSION,
        pattern=r"^\d+\.\d+\.\d+$",
    )
    execution_id: UUID
    contract: ExperimentContract
    plan: IterationPlan
    iterations: tuple[SupportIterationResult, ...] = Field(min_length=2)
    variant_aggregates: tuple[SupportVariantAggregate, ...] = Field(
        min_length=2,
        max_length=2,
    )
    content_hash: str = ""

    @model_validator(mode="after")
    def ensure_content_hash(self) -> SupportExperimentResult:
        """Stamp an omitted hash or reject a hash for different result content."""
        expected_hash = compute_support_experiment_result_hash(self)
        if self.content_hash and self.content_hash != expected_hash:
            raise ExperimentResultIntegrityError(
                "result content hash does not match the assembled result"
            )
        object.__setattr__(self, "content_hash", expected_hash)
        return self


class SupportSuiteExperimentResult(BaseModel):
    """A complete, neutral, hash-verified v2 result for one support-suite experiment."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["2.0.0"] = SUPPORT_SUITE_EXPERIMENT_RESULT_SCHEMA_VERSION
    execution_id: UUID
    contract: SupportSuiteExperimentContract
    plan: IterationPlan
    iterations: tuple[SupportIterationResult, ...] = Field(min_length=18)
    case_aggregates: tuple[SupportSuiteCaseAggregate, ...] = Field(min_length=3)
    pooled_variant_aggregates: tuple[SupportVariantAggregate, ...] = Field(
        min_length=2,
        max_length=2,
    )
    content_hash: str = ""

    @model_validator(mode="before")
    @classmethod
    def reject_unsanitized_iteration_payloads(cls, value: object) -> object:
        """Reject raw response content rather than silently normalizing a v2 result payload."""
        if not isinstance(value, Mapping):
            return value
        iterations = value.get("iterations")
        if not isinstance(iterations, (list, tuple)):
            return value
        for iteration in iterations:
            if isinstance(iteration, SupportIterationResult):
                run_value: object = iteration.run
            elif isinstance(iteration, Mapping):
                run_value = iteration.get("run")
            else:
                continue
            try:
                run = (
                    run_value
                    if isinstance(run_value, SimulationRun)
                    else SimulationRun.model_validate(run_value)
                )
            except ValueError:
                continue
            _require_scrubbed_model_response(run)
        return value

    @model_validator(mode="after")
    def ensure_complete_suite_evidence(self) -> "SupportSuiteExperimentResult":
        """Reject altered suite evidence before it can become a published result."""
        expected_plan = build_iteration_plan(self.contract)
        if self.plan != expected_plan:
            raise ExperimentResultIntegrityError(
                "result iteration plan differs from the support-suite contract schedule"
            )

        expected_identities = expected_plan.iterations
        observed_identities = tuple(iteration.identity for iteration in self.iterations)
        if observed_identities != expected_identities:
            raise ExperimentResultIntegrityError(
                "result iterations are missing, duplicated, or differ from the planned identities"
            )

        for iteration in self.iterations:
            validate_support_suite_iteration_evidence(
                self.contract,
                iteration.identity,
                iteration,
            )

        expected_case_aggregates = _suite_case_aggregates(self.contract, self.iterations)
        if self.case_aggregates != expected_case_aggregates:
            raise ExperimentResultIntegrityError(
                "case aggregates differ from the complete suite iteration evidence"
            )
        expected_pooled_aggregates = _variant_aggregates(self.iterations)
        if self.pooled_variant_aggregates != expected_pooled_aggregates:
            raise ExperimentResultIntegrityError(
                "pooled variant aggregates differ from the complete suite iteration evidence"
            )

        expected_hash = compute_support_suite_experiment_result_hash(self)
        if self.content_hash and self.content_hash != expected_hash:
            raise ExperimentResultIntegrityError(
                "result content hash does not match the assembled result"
            )
        object.__setattr__(self, "content_hash", expected_hash)
        return self


type SupportResult = SupportExperimentResult | SupportSuiteExperimentResult


def assemble_support_experiment_result(
    contract: ExperimentContract,
    execution: ExperimentExecution[SimulationRun],
    *,
    scenario: CompiledSupportScenario,
) -> SupportExperimentResult:
    """Publish a complete neutral support result only when every identity agrees.

    The compiled scenario supplies the support bundle identity that a legacy
    ``SimulationRun`` records. The generic engine intentionally does not know
    that representation.
    """
    _validate_support_contract(contract)
    _validate_compiled_scenario(contract, scenario)
    completed = _validate_execution(contract, execution)

    iteration_results: list[SupportIterationResult] = []
    for expected_identity, record in zip(execution.plan.iterations, completed, strict=True):
        run = scrub_model_response(record.result)
        _validate_run_identity(contract, scenario, run)
        _validate_evaluator_references(contract.evaluators, run.evaluators)
        iteration_results.append(
            SupportIterationResult(
                identity=expected_identity,
                run=run,
                attributed_mutations=tuple(
                    SupportMutationAttribution(mutation=mutation) for mutation in run.mutations
                ),
            )
        )

    return SupportExperimentResult(
        execution_id=execution.execution_id,
        contract=contract,
        plan=execution.plan,
        iterations=tuple(iteration_results),
        variant_aggregates=_variant_aggregates(iteration_results),
    )


def assemble_support_suite_experiment_result(
    contract: SupportSuiteExperimentContract,
    execution: ExperimentExecution[SimulationRun],
    *,
    scenarios: Iterable[CompiledSupportScenario],
) -> SupportSuiteExperimentResult:
    """Publish complete v2 suite evidence only when every case and run agrees.

    Each compiled scenario fixes the bundle ID that a legacy ``SimulationRun``
    reports. The result retains the exact suite contract and every ordered
    iteration, then derives per-case and explicitly pooled measurements.
    """
    _validate_support_contract(contract)
    compiled_by_reference = _validate_compiled_suite_scenarios(contract, scenarios)
    completed = _validate_execution(contract, execution)

    iteration_results: list[SupportIterationResult] = []
    for expected_identity, record in zip(execution.plan.iterations, completed, strict=True):
        run = scrub_model_response(record.result)
        scenario = compiled_by_reference[expected_identity.scenario]
        _validate_suite_run_identity(expected_identity, scenario, run)
        _validate_evaluator_references(contract.evaluators, run.evaluators)
        iteration_results.append(
            SupportIterationResult(
                identity=expected_identity,
                run=run,
                attributed_mutations=tuple(
                    SupportMutationAttribution(mutation=mutation) for mutation in run.mutations
                ),
            )
        )

    iterations = tuple(iteration_results)
    return SupportSuiteExperimentResult(
        execution_id=execution.execution_id,
        contract=contract,
        plan=execution.plan,
        iterations=iterations,
        case_aggregates=_suite_case_aggregates(contract, iterations),
        pooled_variant_aggregates=_variant_aggregates(iterations),
    )


def scrub_model_response(run: SimulationRun) -> SimulationRun:
    """Retain only typed response facts and no model-controlled response content."""
    if run.response is None:
        return run
    response = run.response.model_copy(
        update={
            "message": REDACTED_MODEL_RESPONSE,
            "context": AnswerContext(
                routing=RoutingDecision(
                    intent=run.response.intent,
                    confidence=0.0,
                )
            ),
        }
    )
    return run.model_copy(update={"response": response})


def _require_scrubbed_model_response(run: SimulationRun) -> None:
    """Reject a run whose response differs from the safe persistence projection."""
    if run != scrub_model_response(run):
        raise ExperimentResultIntegrityError(
            "support-suite iteration evidence contains an unsanitized model response"
        )


def compute_support_experiment_result_hash(result: SupportExperimentResult) -> str:
    """Return the canonical SHA-256 hash of result content without its own hash."""
    payload = result.model_dump(mode="json", exclude={"content_hash"})
    canonical = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return sha256(canonical.encode("utf-8")).hexdigest()


def compute_support_suite_experiment_result_hash(result: SupportSuiteExperimentResult) -> str:
    """Return the canonical SHA-256 hash of v2 result content without its own hash."""
    payload = result.model_dump(mode="json", exclude={"content_hash"})
    canonical = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return sha256(canonical.encode("utf-8")).hexdigest()


def compute_support_result_hash(result: SupportResult) -> str:
    """Return the version-specific canonical hash for either durable support result."""
    if isinstance(result, SupportSuiteExperimentResult):
        return compute_support_suite_experiment_result_hash(result)
    return compute_support_experiment_result_hash(result)


def parse_support_experiment_result(
    payload: Mapping[str, object] | str | bytes | bytearray,
) -> SupportResult:
    """Parse an external support result by its explicit result schema version."""
    raw_payload: object = (
        json.loads(payload) if isinstance(payload, (str, bytes, bytearray)) else payload
    )
    if not isinstance(raw_payload, Mapping):
        raise ValueError("support experiment result payload must be an object")

    schema_version = raw_payload.get("schema_version", SUPPORT_EXPERIMENT_RESULT_SCHEMA_VERSION)
    if schema_version == SUPPORT_EXPERIMENT_RESULT_SCHEMA_VERSION:
        return SupportExperimentResult.model_validate(raw_payload)
    if schema_version == SUPPORT_SUITE_EXPERIMENT_RESULT_SCHEMA_VERSION:
        return SupportSuiteExperimentResult.model_validate(raw_payload)
    raise ValueError(f"unsupported support experiment result schema_version {schema_version!r}")


def validate_support_suite_iteration_evidence(
    contract: SupportSuiteExperimentContract,
    identity: IterationIdentity,
    evidence: SupportIterationResult,
) -> None:
    """Require one stored v2 iteration to match its scenario, evaluators, and redaction."""
    if evidence.identity != identity:
        raise ExperimentResultIntegrityError(
            "iteration evidence differs from the support-suite contract schedule"
        )
    _validate_run_scenario_identity(identity.scenario, evidence.run)
    _validate_evaluator_references(contract.evaluators, evidence.run.evaluators)
    _require_scrubbed_model_response(evidence.run)


def _validate_support_contract(
    contract: ExperimentContract | SupportSuiteExperimentContract,
) -> None:
    """Require the reviewed support world and plugin before adapting support runs."""
    from app.domain.world.support import support_world_contract

    if contract.world != support_world_contract().identity:
        raise ExperimentResultIntegrityError(
            "contract does not identify the approved support world"
        )
    if contract.plugin.plugin_id != "support":
        raise ExperimentResultIntegrityError("contract does not identify a support world plugin")


def _validate_compiled_scenario(
    contract: ExperimentContract,
    scenario: CompiledSupportScenario,
) -> None:
    """Require the exact compiled bundle and scenario reference fixed by the contract."""
    bundle = scenario.load_bundle()
    if bundle.bundle_id is None or bundle.content_hash is None:
        raise ExperimentResultIntegrityError("compiled support scenario has no bundle identity")
    if scenario.reference != contract.scenario:
        raise ExperimentResultIntegrityError(
            "compiled support scenario identity differs from the experiment contract"
        )
    if scenario.plugin_ref != contract.plugin:
        raise ExperimentResultIntegrityError(
            "compiled support plugin identity differs from the experiment contract"
        )
    if bundle.content_hash != contract.scenario.content_hash:
        raise ExperimentResultIntegrityError(
            "compiled support bundle identity differs from the experiment contract"
        )


def _validate_compiled_suite_scenarios(
    contract: SupportSuiteExperimentContract,
    scenarios: Iterable[CompiledSupportScenario],
) -> dict[ScenarioRef, CompiledSupportScenario]:
    """Require exactly one approved compiled scenario for every immutable suite member."""
    compiled_scenarios = tuple(scenarios)
    expected_references = tuple(member.scenario for member in contract.suite.members)
    by_reference = {scenario.reference: scenario for scenario in compiled_scenarios}
    if (
        len(compiled_scenarios) != len(expected_references)
        or len(by_reference) != len(compiled_scenarios)
        or set(by_reference) != set(expected_references)
    ):
        raise ExperimentResultIntegrityError(
            "compiled support scenarios differ from the support-suite members"
        )
    for member in contract.suite.members:
        scenario = by_reference[member.scenario]
        _validate_compiled_suite_scenario(member.scenario, contract, scenario)
    return by_reference


def _validate_compiled_suite_scenario(
    reference: ScenarioRef,
    contract: SupportSuiteExperimentContract,
    scenario: CompiledSupportScenario,
) -> None:
    """Require one compiled scenario to match its suite member and support plugin."""
    bundle = scenario.load_bundle()
    if bundle.bundle_id is None or bundle.content_hash is None:
        raise ExperimentResultIntegrityError("compiled support scenario has no bundle identity")
    if scenario.reference != reference:
        raise ExperimentResultIntegrityError(
            "compiled support scenario identity differs from the support-suite member"
        )
    if scenario.plugin_ref != contract.plugin:
        raise ExperimentResultIntegrityError(
            "compiled support plugin identity differs from the experiment contract"
        )
    if bundle.content_hash != reference.content_hash:
        raise ExperimentResultIntegrityError(
            "compiled support bundle identity differs from the support-suite member"
        )


def _validate_execution(
    contract: ExperimentContract | SupportSuiteExperimentContract,
    execution: ExperimentExecution[SimulationRun],
) -> tuple[CompletedIteration[SimulationRun], ...]:
    """Reject incomplete, altered, duplicate, or re-ordered scheduled execution evidence."""
    expected_plan = build_iteration_plan(contract)
    if execution.contract_hash != contract.content_hash:
        raise ExperimentResultIntegrityError("execution contract hash differs from the contract")
    if execution.plan != expected_plan:
        raise ExperimentResultIntegrityError("execution plan differs from the contract schedule")
    if len(execution.iterations) != len(expected_plan.iterations):
        raise ExperimentResultIntegrityError(
            "execution is missing or duplicates scheduled iterations"
        )

    completed: list[CompletedIteration[SimulationRun]] = []
    for expected_identity, record in zip(
        expected_plan.iterations,
        execution.iterations,
        strict=True,
    ):
        if record.identity != expected_identity:
            raise ExperimentResultIntegrityError(
                "execution iterations are missing, duplicated, or out of order"
            )
        if not isinstance(record, CompletedIteration):
            raise ExperimentResultIntegrityError(
                "a scheduled iteration failed and cannot be published"
            )
        completed.append(record)
    return tuple(completed)


def _validate_run_identity(
    contract: ExperimentContract,
    scenario: CompiledSupportScenario,
    run: SimulationRun,
) -> None:
    """Require every support run to be evidence from the exact contracted bundle."""
    bundle = scenario.load_bundle()
    mismatches: list[str] = []
    if run.bundle_id != bundle.bundle_id:
        mismatches.append("bundle_id")
    if run.bundle_content_hash != contract.scenario.content_hash:
        mismatches.append("bundle_content_hash")
    if run.scenario_id != bundle.scenario.scenario_id:
        mismatches.append("scenario_id")
    if mismatches:
        raise ExperimentResultIntegrityError(
            "support run bundle/scenario identity differs from the contract: "
            f"{', '.join(mismatches)}"
        )


def _validate_suite_run_identity(
    identity: IterationIdentity,
    scenario: CompiledSupportScenario,
    run: SimulationRun,
) -> None:
    """Require every v2 run to identify the exact compiled bundle for its case."""
    bundle = scenario.load_bundle()
    if bundle.bundle_id is None:
        raise ExperimentResultIntegrityError("compiled support scenario has no bundle identity")
    mismatches: list[str] = []
    if run.bundle_id != bundle.bundle_id:
        mismatches.append("bundle_id")
    if run.bundle_content_hash != identity.scenario.content_hash:
        mismatches.append("bundle_content_hash")
    if run.scenario_id != bundle.scenario.scenario_id:
        mismatches.append("scenario_id")
    if mismatches:
        raise ExperimentResultIntegrityError(
            "support run bundle/scenario identity differs from the support-suite member: "
            f"{', '.join(mismatches)}"
        )


def _validate_run_scenario_identity(
    scenario: ScenarioRef,
    run: SimulationRun,
) -> None:
    """Require persisted v2 evidence to retain the planned bundle content identity."""
    mismatches: list[str] = []
    if run.bundle_content_hash != scenario.content_hash:
        mismatches.append("bundle_content_hash")
    if mismatches:
        raise ExperimentResultIntegrityError(
            "support run bundle identity differs from the support-suite member: "
            f"{', '.join(mismatches)}"
        )


def _validate_evaluator_references(
    references: tuple[EvaluatorRef, ...],
    report: EvaluatorReport,
) -> None:
    """Require each declared evaluator ID and version exactly once in every run."""
    declared = {(reference.evaluator_id, reference.version) for reference in references}
    observed = tuple((result.evaluator, result.version) for result in report.results)
    if len(observed) != len(set(observed)) or set(observed) != declared:
        raise ExperimentResultIntegrityError(
            "run evaluator IDs or versions differ from the declared evaluator references"
        )


def _variant_aggregates(
    iterations: Iterable[SupportIterationResult],
) -> tuple[SupportVariantAggregate, ...]:
    """Calculate per-variant statistics while leaving run-level values authoritative."""
    measurements_by_variant: dict[ConfigurationVariant, dict[str, list[float]]] = {
        ConfigurationVariant.BASELINE: defaultdict(list),
        ConfigurationVariant.CANDIDATE: defaultdict(list),
    }
    for iteration in iterations:
        measurements = measurements_by_variant[iteration.identity.variant]
        for name, value in _numeric_measurements(iteration.run).items():
            measurements[name].append(value)

    return tuple(
        SupportVariantAggregate(
            variant=variant,
            measurements={
                name: _aggregate(values)
                for name, values in sorted(measurements_by_variant[variant].items())
            },
        )
        for variant in (ConfigurationVariant.BASELINE, ConfigurationVariant.CANDIDATE)
    )


def _suite_case_aggregates(
    contract: SupportSuiteExperimentContract,
    iterations: Iterable[SupportIterationResult],
) -> tuple[SupportSuiteCaseAggregate, ...]:
    """Calculate one baseline/candidate aggregate pair for every exact suite member."""
    iteration_records = tuple(iterations)
    return tuple(
        SupportSuiteCaseAggregate(
            scenario=member.scenario,
            variant_aggregates=_variant_aggregates(
                iteration
                for iteration in iteration_records
                if iteration.identity.scenario == member.scenario
            ),
        )
        for member in contract.suite.members
    )


def _numeric_measurements(run: SimulationRun) -> dict[str, float]:
    """Return the support-run numeric observations whose source reported a value."""
    measurements: dict[str, float] = {
        "retries": float(run.retries),
        "tool_call_count": float(len(run.tool_calls)),
    }
    for name in ("total_latency_ms", "model_latency_ms", "cost_usd"):
        value = getattr(run, name)
        if value is not None:
            measurements[name] = float(value)
    if run.total_tokens > 0:
        measurements.update(
            {
                "input_tokens": float(run.input_tokens),
                "output_tokens": float(run.output_tokens),
                "total_tokens": float(run.total_tokens),
            }
        )
    for result in run.evaluators.results:
        for name, value in result.measured.items():
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                measurements[f"evaluator.{result.evaluator}.{name}"] = float(value)
    return measurements


def _aggregate(values: list[float]) -> NumericMeasurementAggregate:
    """Return descriptive statistics using population, rather than sample, spread."""
    return NumericMeasurementAggregate(
        count=len(values),
        mean=sum(values) / len(values),
        median=float(median(values)),
        min=min(values),
        max=max(values),
        population_standard_deviation=float(pstdev(values)),
    )
