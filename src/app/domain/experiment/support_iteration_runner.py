"""Execute one support experiment iteration with pinned runtime inputs.

This adapter is deliberately narrow. It bridges the generic experiment engine
to the existing support ``run_bundle`` implementation without adding a second
simulation behavior, persistence path, or execution provider.
"""

import asyncio
import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path

import app
from app.adapters.pydantic_ai_agent import ModelConfig
from app.domain.bundle.schemas import SimulationBundle
from app.domain.comparison import evaluators as support_evaluators
from app.domain.comparison.evaluators import ALL_EVALUATORS, Evaluator
from app.domain.experiment.contracts import (
    BUILTIN_SUPPORT_RUNTIME_ARTIFACT_ID,
    BUILTIN_SUPPORT_RUNTIME_REFERENCE,
    AgentConfiguration,
    ArtifactKind,
    ArtifactRef,
    ConfigurationVariant,
    EvaluatorRef,
    ExecutionLimits,
    ExperimentContract,
    IterationIdentity,
    ModelRef,
    PromptRef,
    SupportSuiteExperimentContract,
    build_iteration_plan,
)
from app.domain.experiment.engine import (
    ExperimentExecutionContext,
    IterationRunner,
)
from app.domain.experiment.errors import IterationExecutionError
from app.domain.simulation.provisioner import (
    EnvironmentRequest,
    ProvisionerFactory,
    SupportEnvironmentProvisioner,
)
from app.domain.simulation.runner import SimulationRun, run_bundle, scenario_from_bundle
from app.domain.world import support as support_world
from app.domain.world.compiler import CompiledWorld
from app.domain.world.support import CompiledSupportScenario, SupportWorldPlugin
from app.errors import DomainError


@dataclass(frozen=True)
class PinnedModelRuntime:
    """One injected model client and the immutable model identity it represents."""

    reference: ModelRef
    model_config: ModelConfig


@dataclass(frozen=True)
class PinnedPromptRuntime:
    """One injected prompt body and the immutable prompt identity it represents."""

    reference: PromptRef
    answer_instructions: str


@dataclass(frozen=True)
class PinnedSupportEvaluator:
    """One injected canonical evaluator and its declared immutable identity."""

    reference: EvaluatorRef
    evaluator: Evaluator


@dataclass(frozen=True)
class SupportRuntimeBindings:
    """All executable inputs injected into the support iteration runner.

    This runner executes only the built-in support runtime. Its source tree is
    attested to ``artifact.content_hash`` before provisioning; OCI and Git
    artifact references are never treated as equivalent to local code. Model
    clients and prompt bodies are supplied here rather than resolved from
    mutable application settings during an iteration.
    """

    artifact: ArtifactRef
    models: tuple[PinnedModelRuntime, ...]
    prompts: tuple[PinnedPromptRuntime, ...]


class SupportIterationRunner(IterationRunner[SimulationRun]):
    """Run one support iteration only after all contracted identities agree."""

    def __init__(
        self,
        *,
        compiled_world: CompiledWorld,
        scenario: CompiledSupportScenario | None = None,
        plugin: SupportWorldPlugin,
        provisioner_factory: ProvisionerFactory,
        runtime: SupportRuntimeBindings,
        evaluators: Sequence[PinnedSupportEvaluator],
    ) -> None:
        self._compiled_world = compiled_world
        self._scenario = scenario
        self._plugin = plugin
        self._provisioner_factory = provisioner_factory
        self._runtime = runtime
        self._evaluators = tuple(evaluators)
        self._environment_ids: set[str] = set()

    async def __call__(
        self,
        context: ExperimentExecutionContext,
        iteration: IterationIdentity,
        configuration: AgentConfiguration,
    ) -> SimulationRun:
        """Execute one configured support run after pre-execution validation."""
        try:
            bundle, model, prompt, evaluators = self._prepare(
                context,
                iteration,
                configuration,
            )
        except IterationExecutionError:
            raise
        except Exception:
            raise IterationExecutionError("iteration_preparation_failed") from None

        try:
            async with asyncio.timeout(context.contract.limits.max_duration_s):
                run = await run_bundle(
                    bundle=bundle,
                    provisioner_factory=self._fresh_provisioner_factory(),
                    model_config=model.model_config,
                    answer_instructions=prompt.answer_instructions,
                    answer_instructions_version=prompt.reference.version,
                    tools_override=bundle.scenario.eligible_actions,
                    evaluators=evaluators,
                )
        except TimeoutError:
            raise IterationExecutionError("duration_limit_reached") from None
        except IterationExecutionError:
            raise
        except DomainError:
            raise IterationExecutionError("simulation_execution_failed") from None
        except Exception:
            raise IterationExecutionError("simulation_execution_failed") from None

        return run

    def _prepare(
        self,
        context: ExperimentExecutionContext,
        iteration: IterationIdentity,
        configuration: AgentConfiguration,
    ) -> tuple[
        SimulationBundle,
        PinnedModelRuntime,
        PinnedPromptRuntime,
        tuple[Evaluator, ...],
    ]:
        """Resolve only runtime values pinned to the selected configuration."""
        contract = context.contract
        scenario = self._resolve_scenario(context, iteration)
        if isinstance(contract, SupportSuiteExperimentContract):
            self._validate_iteration_plan(context, iteration)
        self._validate_runtime_artifact(configuration.artifact)
        _validate_enforceable_limits(contract.limits)
        self._validate_world(context)
        self._validate_scenario_and_plugin(context, iteration, scenario)
        self._validate_selected_configuration(contract, iteration, configuration)
        bundle = self._load_bundle(scenario)
        model = self._resolve_model(configuration)
        prompt = self._resolve_prompt(configuration)
        evaluators = self._resolve_evaluators(contract.evaluators)
        return bundle, model, prompt, evaluators

    def _validate_runtime_artifact(self, artifact: ArtifactRef) -> None:
        """Attest the only executable artifact this local runner can prove."""
        if self._runtime.artifact != artifact:
            raise IterationExecutionError("artifact_identity_mismatch")
        if artifact.kind is not ArtifactKind.BUILTIN_SUPPORT_RUNTIME:
            raise IterationExecutionError("artifact_execution_unattested")
        if (
            artifact.artifact_id != BUILTIN_SUPPORT_RUNTIME_ARTIFACT_ID
            or artifact.reference != BUILTIN_SUPPORT_RUNTIME_REFERENCE
        ):
            raise IterationExecutionError("artifact_identity_mismatch")
        if artifact.content_hash != _attested_hash(builtin_support_runtime_content_hash):
            raise IterationExecutionError("artifact_identity_mismatch")

    def _validate_world(self, context: ExperimentExecutionContext) -> None:
        """Require one exact compiled support world before environment creation."""
        if (
            context.world.identity != context.contract.world
            or self._compiled_world.identity != context.contract.world
            or self._compiled_world != self._plugin.compiled_world
            or context.world != self._compiled_world
        ):
            raise IterationExecutionError("world_identity_mismatch")

    def _resolve_scenario(
        self,
        context: ExperimentExecutionContext,
        iteration: IterationIdentity,
    ) -> CompiledSupportScenario:
        """Resolve the exact v2 scenario named by this iteration."""
        if isinstance(context.contract, SupportSuiteExperimentContract):
            matches = tuple(
                compiled
                for compiled in context.compiled_scenarios
                if isinstance(compiled, CompiledSupportScenario)
                and compiled.reference == iteration.scenario
            )
            if len(matches) != 1:
                raise IterationExecutionError("scenario_identity_mismatch")
            return matches[0]
        if self._scenario is None:
            raise IterationExecutionError("scenario_identity_mismatch")
        return self._scenario

    @staticmethod
    def _validate_iteration_plan(
        context: ExperimentExecutionContext,
        iteration: IterationIdentity,
    ) -> None:
        """Reject an identity not issued by this exact contract plan."""
        plan = context.plan or build_iteration_plan(context.contract)
        if iteration not in plan.iterations:
            raise IterationExecutionError("iteration_plan_identity_mismatch")

    def _validate_scenario_and_plugin(
        self,
        context: ExperimentExecutionContext,
        iteration: IterationIdentity,
        scenario: CompiledSupportScenario,
    ) -> None:
        """Require the compiled support case and attested built-in plugin."""
        contract = context.contract
        expected_scenario = (
            iteration.scenario
            if isinstance(contract, SupportSuiteExperimentContract)
            else contract.scenario
        )
        if (
            scenario.reference != expected_scenario
            or (
                isinstance(contract, SupportSuiteExperimentContract)
                and scenario.reference
                not in tuple(member.scenario for member in contract.suite.members)
            )
            or (
                isinstance(contract, ExperimentContract)
                and scenario.reference != context.scenario
            )
        ):
            raise IterationExecutionError("scenario_identity_mismatch")
        if (
            scenario.plugin_ref != contract.plugin
            or scenario.plugin_ref != context.plugin
            or self._plugin.plugin_ref != contract.plugin
        ):
            raise IterationExecutionError("plugin_identity_mismatch")
        if (
            type(self._plugin) is not SupportWorldPlugin
            or contract.plugin.plugin_id != "support"
            or contract.plugin.version != support_world.SUPPORT_PLUGIN_VERSION
            or contract.plugin.content_hash != _attested_hash(builtin_support_plugin_content_hash)
        ):
            raise IterationExecutionError("plugin_identity_mismatch")

    @staticmethod
    def _validate_selected_configuration(
        contract: ExperimentContract | SupportSuiteExperimentContract,
        iteration: IterationIdentity,
        configuration: AgentConfiguration,
    ) -> None:
        """Reject a configuration that was not selected by the iteration variant."""
        if iteration.variant is ConfigurationVariant.BASELINE:
            expected = contract.baseline
        elif iteration.variant is ConfigurationVariant.CANDIDATE:
            expected = contract.candidate
        else:
            raise IterationExecutionError("configuration_identity_mismatch")
        if configuration != expected:
            raise IterationExecutionError("configuration_identity_mismatch")

    def _load_bundle(self, scenario: CompiledSupportScenario) -> SimulationBundle:
        """Validate that the compiled snapshot still matches its scenario reference."""
        bundle = scenario.load_bundle()
        if (
            bundle.bundle_id is None
            or bundle.content_hash != scenario.reference.content_hash
            or _starting_state_hash(bundle) != scenario.reference.starting_state_hash
        ):
            raise IterationExecutionError("scenario_identity_mismatch")
        return bundle

    def _resolve_model(self, configuration: AgentConfiguration) -> PinnedModelRuntime:
        """Resolve exactly one injected model client for the selected model reference."""
        matches = tuple(
            runtime
            for runtime in self._runtime.models
            if runtime.reference == configuration.model
        )
        if len(matches) != 1:
            raise IterationExecutionError("model_identity_mismatch")
        model = matches[0]
        if (
            model.model_config.provider != model.reference.provider
            or model.model_config.name != model.reference.model_id
        ):
            raise IterationExecutionError("model_identity_mismatch")
        return model

    def _resolve_prompt(self, configuration: AgentConfiguration) -> PinnedPromptRuntime:
        """Resolve exactly one injected prompt body for the selected prompt reference."""
        matches = tuple(
            runtime
            for runtime in self._runtime.prompts
            if runtime.reference == configuration.prompt
        )
        if len(matches) != 1 or not matches[0].answer_instructions.strip():
            raise IterationExecutionError("prompt_identity_mismatch")
        prompt = matches[0]
        try:
            prompt_hash = sha256(prompt.answer_instructions.encode("utf-8")).hexdigest()
        except UnicodeEncodeError:
            raise IterationExecutionError("prompt_identity_mismatch") from None
        if prompt_hash != prompt.reference.content_hash:
            raise IterationExecutionError("prompt_identity_mismatch")
        return prompt

    def _resolve_evaluators(
        self,
        references: tuple[EvaluatorRef, ...],
    ) -> tuple[Evaluator, ...]:
        """Require canonical evaluator objects and their loaded source identity."""
        if tuple(binding.reference for binding in self._evaluators) != references:
            raise IterationExecutionError("evaluator_identity_mismatch")
        expected_hash = _attested_hash(builtin_support_evaluator_content_hash)
        canonical_by_id = {evaluator.name: evaluator for evaluator in ALL_EVALUATORS}
        resolved: list[Evaluator] = []
        for binding in self._evaluators:
            evaluator = canonical_by_id.get(binding.reference.evaluator_id)
            if (
                evaluator is None
                or binding.evaluator is not evaluator
                or evaluator.version != binding.reference.version
                or binding.reference.content_hash != expected_hash
            ):
                raise IterationExecutionError("evaluator_identity_mismatch")
            resolved.append(evaluator)
        return tuple(resolved)

    def _fresh_provisioner_factory(self) -> ProvisionerFactory:
        """Reject a factory that tries to reuse an environment across iterations."""

        def provision(request: EnvironmentRequest) -> SupportEnvironmentProvisioner:
            provisioner = self._provisioner_factory(request)
            if (
                not provisioner.environment_id
                or provisioner.environment_id in self._environment_ids
            ):
                raise IterationExecutionError("provisioner_identity_reused")
            self._environment_ids.add(provisioner.environment_id)
            return provisioner

        return provision


def _starting_state_hash(bundle: SimulationBundle) -> str:
    """Return the canonical starting-state identity used by support compilation."""
    state = scenario_from_bundle(bundle).initial_state.model_dump(mode="json")
    canonical = json.dumps(state, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return sha256(canonical.encode()).hexdigest()


def builtin_support_runtime_content_hash() -> str:
    """Return the current process's complete built-in application source-tree hash.

    This is a local source-tree attestation, not an OCI image or Git commit
    attestation. Contracts using another artifact kind are rejected before
    the runner can provision an environment.
    """
    try:
        app_file = app.__file__
        if app_file is None:
            raise _RuntimeAttestationUnavailable
        root = Path(app_file).resolve(strict=True).parent
        source_files = tuple(
            sorted(
                (path for path in root.rglob("*.py") if path.is_file()),
                key=lambda path: path.relative_to(root).as_posix(),
            )
        )
        if not source_files:
            raise _RuntimeAttestationUnavailable
        manifest = [
            (path.relative_to(root).as_posix(), sha256(path.read_bytes()).hexdigest())
            for path in source_files
        ]
    except (OSError, ValueError):
        raise _RuntimeAttestationUnavailable from None
    return _manifest_hash(manifest)


def builtin_support_plugin_content_hash() -> str:
    """Return the loaded built-in support plugin module source hash."""
    return _module_source_hash(support_world.__file__)


def builtin_support_evaluator_content_hash() -> str:
    """Return the loaded deterministic evaluator module source hash."""
    return _module_source_hash(support_evaluators.__file__)


class _RuntimeAttestationUnavailable(Exception):
    """The running process cannot supply the source bytes this runner requires."""


def _module_source_hash(module_file: str | None) -> str:
    """Hash source bytes only when they are available from the executing module."""
    try:
        if module_file is None:
            raise _RuntimeAttestationUnavailable
        source = Path(module_file).resolve(strict=True)
        if source.suffix != ".py":
            raise _RuntimeAttestationUnavailable
        return sha256(source.read_bytes()).hexdigest()
    except (OSError, ValueError):
        raise _RuntimeAttestationUnavailable from None


def _manifest_hash(manifest: list[tuple[str, str]]) -> str:
    """Return a canonical hash for an ordered set of source-file hashes."""
    encoded = json.dumps(manifest, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return sha256(encoded).hexdigest()


def _attested_hash(source_hash: Callable[[], str]) -> str:
    """Return a local executable hash or fail before execution if it is unavailable."""
    try:
        return source_hash()
    except _RuntimeAttestationUnavailable:
        raise IterationExecutionError("runtime_attestation_unavailable") from None


def _validate_enforceable_limits(limits: ExecutionLimits) -> None:
    """Reject every configured ceiling that this ``run_bundle`` seam cannot preempt."""
    support_turns_per_iteration = 1
    if support_turns_per_iteration > limits.max_turns:
        raise IterationExecutionError("turn_limit_reached")
    if limits.max_tokens is not None:
        raise IterationExecutionError("token_limit_unenforceable")
    if limits.max_cost_usd is not None:
        raise IterationExecutionError("cost_limit_unenforceable")
    if limits.max_tool_calls is not None:
        raise IterationExecutionError("tool_call_limit_unenforceable")
    if limits.max_retries is not None:
        raise IterationExecutionError("retry_limit_unenforceable")
