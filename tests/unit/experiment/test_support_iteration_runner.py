"""Focused tests for the pinned support experiment iteration runner."""

import asyncio
from dataclasses import dataclass, replace
from hashlib import sha256
from uuid import UUID, uuid4

import pytest
from tests.fakes.provisioner import stateful_provisioner_factory

from app.adapters.pydantic_ai_agent import ModelConfig
from app.domain.bundle.compiler import compile_bundle
from app.domain.comparison.evaluators import ALL_EVALUATORS
from app.domain.experiment.contracts import (
    BUILTIN_SUPPORT_RUNTIME_ARTIFACT_ID,
    BUILTIN_SUPPORT_RUNTIME_REFERENCE,
    AgentConfiguration,
    ArtifactKind,
    ArtifactRef,
    CandidateChangeType,
    ConfigurationVariant,
    EvaluatorRef,
    ExecutionLimits,
    ExperimentContract,
    InterleavingPlan,
    ModelRef,
    PluginRef,
    PromptRef,
    ScenarioRef,
    SupportSuiteExperimentContract,
    SupportSuiteMemberRef,
    SupportSuiteRef,
    build_iteration_plan,
)
from app.domain.experiment.engine import ExperimentExecutionContext
from app.domain.experiment.errors import IterationExecutionError
from app.domain.experiment.support_iteration_runner import (
    PinnedModelRuntime,
    PinnedPromptRuntime,
    PinnedSupportEvaluator,
    SupportIterationRunner,
    SupportRuntimeBindings,
    builtin_support_evaluator_content_hash,
    builtin_support_plugin_content_hash,
    builtin_support_runtime_content_hash,
)
from app.domain.regression.schemas import CaseSourceType, RegressionCase
from app.domain.simulation.runner import SimulationRun
from app.domain.simulation.scenarios import SCENARIO_BY_ID
from app.domain.suite.schemas import SuiteMemberRef, suite_members_hash
from app.domain.world.compiler import CompiledWorld
from app.domain.world.support import (
    SUPPORT_PLUGIN_VERSION,
    CompiledSupportScenario,
    SupportWorldPlugin,
)


@dataclass(frozen=True)
class SupportRunnerSetup:
    """The minimum compiled support inputs required by each focused test."""

    contract: ExperimentContract
    context: ExperimentExecutionContext
    plugin: SupportWorldPlugin
    scenario: CompiledSupportScenario
    runtime: SupportRuntimeBindings
    evaluators: tuple[PinnedSupportEvaluator, ...]

    @property
    def iteration(self):
        """Return the first scheduled baseline iteration."""
        return build_iteration_plan(self.contract).iterations[0]


ANSWER_INSTRUCTIONS = "Use the reviewed support-answer prompt."


def _compiled_support_scenario(
    plugin_ref: PluginRef,
) -> tuple[SupportWorldPlugin, CompiledSupportScenario]:
    bundle = compile_bundle(
        scenario=SCENARIO_BY_ID["phase2-01-bad-prompt-policy-answer"],
        approved_request_message="What does the approved refund policy allow?",
        reviewer="alice",
        reviewed_at="2026-08-08T00:00:00Z",
        reason="Approved for the experiment runner tests.",
        review_status="approved",
    )
    assert bundle.content_hash is not None
    case = RegressionCase(
        case_id=uuid4(),
        case_version=1,
        source_type=CaseSourceType.DESIGNED_EDGE_CASE,
        scenario_id=bundle.scenario.scenario_id,
        bundle=bundle,
        bundle_content_hash=bundle.content_hash,
        evidence_ref=bundle.evidence_ref,
        evidence_content_hash=bundle.evidence_content_hash,
        configuration_versions=bundle.configuration_versions,
        created_at="2026-08-08T00:00:00Z",
    )
    plugin = SupportWorldPlugin(plugin_ref)
    return plugin, plugin.compile_case(case)


@pytest.fixture
def setup() -> SupportRunnerSetup:
    """Build one exact compiled support contract without opening an environment."""
    plugin_ref = PluginRef(
        plugin_id="support",
        version=SUPPORT_PLUGIN_VERSION,
        content_hash=builtin_support_plugin_content_hash(),
    )
    plugin, scenario = _compiled_support_scenario(plugin_ref)
    artifact = ArtifactRef(
        artifact_id=BUILTIN_SUPPORT_RUNTIME_ARTIFACT_ID,
        kind=ArtifactKind.BUILTIN_SUPPORT_RUNTIME,
        reference=BUILTIN_SUPPORT_RUNTIME_REFERENCE,
        content_hash=builtin_support_runtime_content_hash(),
    )
    prompt = PromptRef(
        prompt_id="support-answer",
        version="1.0.0",
        content_hash=sha256(ANSWER_INSTRUCTIONS.encode("utf-8")).hexdigest(),
    )
    baseline_model = ModelRef(
        provider="openai",
        model_id="gpt-5.2",
        version="2026-08-01",
    )
    candidate_model = ModelRef(
        provider="openai",
        model_id="gpt-5.3",
        version="2026-09-01",
    )
    baseline = AgentConfiguration(
        artifact=artifact,
        model=baseline_model,
        prompt=prompt,
    )
    candidate = AgentConfiguration(
        artifact=artifact,
        model=candidate_model,
        prompt=prompt,
    )
    evaluator_ref = EvaluatorRef(
        evaluator_id="authorization",
        version="1.0.0",
        content_hash=builtin_support_evaluator_content_hash(),
    )
    contract = ExperimentContract(
        experiment_id=uuid4(),
        world=plugin.compiled_world.identity,
        scenario=scenario.reference,
        plugin=plugin_ref,
        evaluators=(evaluator_ref,),
        baseline=baseline,
        candidate=candidate,
        candidate_change=CandidateChangeType.MODEL,
        limits=ExecutionLimits(
            max_turns=1,
            max_duration_s=1,
        ),
        interleaving=InterleavingPlan(repetitions=3, seed=7),
    )
    context = ExperimentExecutionContext(
        contract=contract,
        world=plugin.compiled_world,
        scenario=scenario.reference,
        plugin=plugin_ref,
    )
    runtime = SupportRuntimeBindings(
        artifact=artifact,
        models=(
            PinnedModelRuntime(
                reference=baseline_model,
                model_config=ModelConfig(provider="openai", name="gpt-5.2"),
            ),
            PinnedModelRuntime(
                reference=candidate_model,
                model_config=ModelConfig(provider="openai", name="gpt-5.3"),
            ),
        ),
        prompts=(
            PinnedPromptRuntime(
                reference=prompt,
                answer_instructions=ANSWER_INSTRUCTIONS,
            ),
        ),
    )
    evaluators = (
        PinnedSupportEvaluator(reference=evaluator_ref, evaluator=ALL_EVALUATORS[0]),
    )
    return SupportRunnerSetup(
        contract=contract,
        context=context,
        plugin=plugin,
        scenario=scenario,
        runtime=runtime,
        evaluators=evaluators,
    )


def _runner(
    setup: SupportRunnerSetup,
    *,
    context: ExperimentExecutionContext | None = None,
    compiled_world: CompiledWorld | None = None,
    scenario: CompiledSupportScenario | None = None,
    plugin: SupportWorldPlugin | None = None,
    runtime: SupportRuntimeBindings | None = None,
    evaluators: tuple[PinnedSupportEvaluator, ...] | None = None,
) -> tuple[SupportIterationRunner, ExperimentExecutionContext]:
    selected_plugin = plugin or setup.plugin
    return (
        SupportIterationRunner(
            compiled_world=compiled_world or selected_plugin.compiled_world,
            scenario=scenario or setup.scenario,
            plugin=selected_plugin,
            provisioner_factory=stateful_provisioner_factory(),
            runtime=runtime or setup.runtime,
            evaluators=evaluators or setup.evaluators,
        ),
        context or setup.context,
    )


def _suite_context(
    setup: SupportRunnerSetup,
) -> tuple[SupportSuiteExperimentContract, ExperimentExecutionContext]:
    """Build a three-member v2 suite context with only exact compiled scenarios."""
    compiled_scenarios = (
        setup.scenario,
        _compiled_support_scenario(setup.contract.plugin)[1],
        _compiled_support_scenario(setup.contract.plugin)[1],
    )
    members = tuple(
        SupportSuiteMemberRef(
            case_id=UUID(hex=scenario.reference.scenario_id.removeprefix("case.")),
            case_version=int(scenario.reference.version),
            scenario=scenario.reference,
        )
        for scenario in compiled_scenarios
    )
    suite = SupportSuiteRef(
        suite_id=uuid4(),
        suite_version=1,
        members_hash=suite_members_hash(
            tuple(
                SuiteMemberRef(case_id=member.case_id, case_version=member.case_version)
                for member in members
            )
        ),
        members=members,
    )
    contract = SupportSuiteExperimentContract(
        experiment_id=setup.contract.experiment_id,
        world=setup.contract.world,
        suite=suite,
        plugin=setup.contract.plugin,
        evaluators=setup.contract.evaluators,
        baseline=setup.contract.baseline,
        candidate=setup.contract.candidate,
        candidate_change=setup.contract.candidate_change,
        limits=setup.contract.limits,
        interleaving=setup.contract.interleaving,
    )
    plan = build_iteration_plan(contract)
    return contract, ExperimentExecutionContext(
        contract=contract,
        world=setup.plugin.compiled_world,
        plugin=setup.plugin.plugin_ref,
        compiled_scenarios=compiled_scenarios,
        plan=plan,
    )


def _configuration_for_iteration(
    contract: SupportSuiteExperimentContract,
    iteration_variant: ConfigurationVariant,
) -> AgentConfiguration:
    """Select the contract configuration matching an iteration variant."""
    if iteration_variant is ConfigurationVariant.BASELINE:
        return contract.baseline
    return contract.candidate


async def test_rejects_unknown_v2_scenario_before_opening_the_environment(
    setup: SupportRunnerSetup,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A v2 iteration cannot name a scenario outside the compiled suite snapshot."""
    contract, context = _suite_context(setup)
    plan = context.plan
    assert plan is not None
    original = plan.iterations[0]
    unknown = original.model_copy(
        update={
            "scenario": ScenarioRef(
                scenario_id=f"case.{uuid4().hex}",
                version="1",
                content_hash="a" * 64,
                starting_state_hash="b" * 64,
            )
        }
    )
    runner, _ = _runner(setup)
    calls: list[dict[str, object]] = []

    async def must_not_open_environment(**kwargs: object) -> SimulationRun:
        calls.append(kwargs)
        raise AssertionError("run_bundle must not open an environment for an unknown scenario")

    monkeypatch.setattr(
        "app.domain.experiment.support_iteration_runner.run_bundle",
        must_not_open_environment,
    )

    with pytest.raises(IterationExecutionError) as raised:
        await runner(
            context,
            unknown,
            _configuration_for_iteration(contract, original.variant),
        )

    assert raised.value.code == "scenario_identity_mismatch"
    assert calls == []


async def test_rejects_swapped_v2_scenario_not_issued_by_the_exact_plan(
    setup: SupportRunnerSetup,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A known suite member cannot replace the scenario fixed in an iteration ID."""
    contract, context = _suite_context(setup)
    plan = context.plan
    assert plan is not None
    original = plan.iterations[0]
    replacement = next(
        iteration.scenario
        for iteration in plan.iterations
        if iteration.scenario != original.scenario
    )
    swapped = original.model_copy(update={"scenario": replacement})
    runner, _ = _runner(setup)
    calls: list[dict[str, object]] = []

    async def must_not_open_environment(**kwargs: object) -> SimulationRun:
        calls.append(kwargs)
        raise AssertionError("run_bundle must not open an environment for a swapped scenario")

    monkeypatch.setattr(
        "app.domain.experiment.support_iteration_runner.run_bundle",
        must_not_open_environment,
    )

    with pytest.raises(IterationExecutionError) as raised:
        await runner(
            context,
            swapped,
            _configuration_for_iteration(contract, original.variant),
        )

    assert raised.value.code == "iteration_plan_identity_mismatch"
    assert calls == []


@pytest.mark.parametrize(
    ("mismatch", "error_code"),
    [
        ("selected_configuration", "configuration_identity_mismatch"),
        ("artifact", "artifact_identity_mismatch"),
        ("model", "model_identity_mismatch"),
        ("prompt", "prompt_identity_mismatch"),
        ("world", "world_identity_mismatch"),
        ("scenario", "scenario_identity_mismatch"),
        ("plugin", "plugin_identity_mismatch"),
        ("evaluator", "evaluator_identity_mismatch"),
    ],
)
async def test_rejects_identity_mismatches_before_opening_the_environment(
    setup: SupportRunnerSetup,
    mismatch: str,
    error_code: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every runtime dependency must agree with the selected immutable contract."""
    runner, context = _runner(setup)
    configuration = setup.contract.baseline
    if mismatch == "selected_configuration":
        configuration = setup.contract.candidate
    elif mismatch == "artifact":
        runner, context = _runner(
            setup,
            runtime=replace(
                setup.runtime,
                artifact=setup.runtime.artifact.model_copy(update={"content_hash": "d" * 64}),
            ),
        )
    elif mismatch == "model":
        runner, context = _runner(
            setup,
            runtime=replace(
                setup.runtime,
                models=(
                    PinnedModelRuntime(
                        reference=setup.contract.baseline.model,
                        model_config=ModelConfig(provider="openai", name="other-model"),
                    ),
                    setup.runtime.models[1],
                ),
            ),
        )
    elif mismatch == "prompt":
        runner, context = _runner(
            setup,
            runtime=replace(
                setup.runtime,
                prompts=(
                    PinnedPromptRuntime(
                        reference=setup.contract.baseline.prompt,
                        answer_instructions="Use an unreviewed support-answer prompt.",
                    ),
                ),
            ),
        )
    elif mismatch == "world":
        context = ExperimentExecutionContext(
            contract=setup.contract,
            world=CompiledWorld(identity=setup.contract.world, actors=()),
            scenario=setup.contract.scenario,
            plugin=setup.contract.plugin,
        )
    elif mismatch == "scenario":
        runner, context = _runner(
            setup,
            scenario=setup.scenario.model_copy(
                update={
                    "reference": setup.scenario.reference.model_copy(
                        update={"content_hash": "d" * 64}
                    )
                }
            ),
        )
    elif mismatch == "plugin":
        false_plugin_ref = setup.contract.plugin.model_copy(update={"content_hash": "d" * 64})
        plugin, scenario = _compiled_support_scenario(false_plugin_ref)
        contract = setup.contract.model_copy(
            update={"plugin": false_plugin_ref, "scenario": scenario.reference}
        )
        context = ExperimentExecutionContext(
            contract=contract,
            world=plugin.compiled_world,
            scenario=scenario.reference,
            plugin=false_plugin_ref,
        )
        runner, context = _runner(
            setup,
            context=context,
            scenario=scenario,
            plugin=plugin,
        )
    elif mismatch == "evaluator":
        false_evaluator_ref = setup.contract.evaluators[0].model_copy(
            update={"content_hash": "d" * 64}
        )
        contract = setup.contract.model_copy(update={"evaluators": (false_evaluator_ref,)})
        context = ExperimentExecutionContext(
            contract=contract,
            world=setup.plugin.compiled_world,
            scenario=contract.scenario,
            plugin=contract.plugin,
        )
        runner, context = _runner(
            setup,
            context=context,
            evaluators=(
                PinnedSupportEvaluator(
                    reference=false_evaluator_ref,
                    evaluator=ALL_EVALUATORS[0],
                ),
            ),
        )
    else:
        raise AssertionError(f"unhandled mismatch {mismatch!r}")

    calls: list[dict[str, object]] = []

    async def must_not_open_environment(**kwargs: object) -> SimulationRun:
        calls.append(kwargs)
        raise AssertionError("run_bundle must not be called after identity mismatch")

    monkeypatch.setattr(
        "app.domain.experiment.support_iteration_runner.run_bundle",
        must_not_open_environment,
    )

    with pytest.raises(IterationExecutionError) as raised:
        await runner(context, setup.iteration, configuration)

    assert raised.value.code == error_code
    assert calls == []


async def test_rejects_unattested_oci_artifact_before_opening_the_environment(
    setup: SupportRunnerSetup,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Local support code must not be presented as a pinned OCI image."""
    digest = "a" * 64
    unattested_artifact = ArtifactRef(
        artifact_id="support-agent",
        kind=ArtifactKind.OCI_IMAGE,
        reference=f"ghcr.io/simulate/support-agent@sha256:{digest}",
        content_hash=digest,
    )
    baseline = setup.contract.baseline.model_copy(update={"artifact": unattested_artifact})
    candidate = setup.contract.candidate.model_copy(update={"artifact": unattested_artifact})
    contract = setup.contract.model_copy(update={"baseline": baseline, "candidate": candidate})
    context = ExperimentExecutionContext(
        contract=contract,
        world=setup.plugin.compiled_world,
        scenario=contract.scenario,
        plugin=contract.plugin,
    )
    runner, context = _runner(
        setup,
        context=context,
        runtime=replace(setup.runtime, artifact=unattested_artifact),
    )
    calls: list[dict[str, object]] = []

    async def must_not_open_environment(**kwargs: object) -> SimulationRun:
        calls.append(kwargs)
        raise AssertionError("run_bundle must not run an unattested OCI artifact")

    monkeypatch.setattr(
        "app.domain.experiment.support_iteration_runner.run_bundle",
        must_not_open_environment,
    )

    with pytest.raises(IterationExecutionError) as raised:
        await runner(context, build_iteration_plan(contract).iterations[0], contract.baseline)

    assert raised.value.code == "artifact_execution_unattested"
    assert calls == []


async def test_rejects_configured_token_budget_before_opening_the_environment(
    setup: SupportRunnerSetup,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A ceiling without a preemptive ``run_bundle`` control cannot be overspent."""
    limits = setup.contract.limits.model_copy(update={"max_tokens": 10})
    contract = setup.contract.model_copy(update={"limits": limits})
    context = ExperimentExecutionContext(
        contract=contract,
        world=setup.plugin.compiled_world,
        scenario=contract.scenario,
        plugin=contract.plugin,
    )
    runner, context = _runner(setup, context=context)
    calls: list[dict[str, object]] = []

    async def must_not_open_environment(**kwargs: object) -> SimulationRun:
        calls.append(kwargs)
        raise AssertionError("run_bundle must not run with an unenforceable token budget")

    monkeypatch.setattr(
        "app.domain.experiment.support_iteration_runner.run_bundle",
        must_not_open_environment,
    )

    with pytest.raises(IterationExecutionError) as raised:
        await runner(context, build_iteration_plan(contract).iterations[0], contract.baseline)

    assert raised.value.code == "token_limit_unenforceable"
    assert calls == []


async def test_enforces_the_wall_clock_duration_limit(
    setup: SupportRunnerSetup,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A hung run is cancelled at the contractual duration boundary."""

    async def hang(**kwargs: object) -> SimulationRun:
        del kwargs
        await asyncio.sleep(60)
        raise AssertionError("the duration boundary did not cancel run_bundle")

    monkeypatch.setattr("app.domain.experiment.support_iteration_runner.run_bundle", hang)
    runner, context = _runner(setup)

    with pytest.raises(IterationExecutionError) as raised:
        await runner(context, setup.iteration, setup.contract.baseline)

    assert raised.value.code == "duration_limit_reached"


async def test_hides_unexpected_runner_exception_details(
    setup: SupportRunnerSetup,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unexpected implementation failures surface as a stable code only."""

    async def explode(**kwargs: object) -> SimulationRun:
        del kwargs
        raise RuntimeError("untrusted endpoint detail")

    monkeypatch.setattr("app.domain.experiment.support_iteration_runner.run_bundle", explode)
    runner, context = _runner(setup)

    with pytest.raises(IterationExecutionError) as raised:
        await runner(context, setup.iteration, setup.contract.baseline)

    assert raised.value.code == "simulation_execution_failed"
    assert "untrusted endpoint detail" not in str(raised.value)
