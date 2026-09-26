"""Run a validated experiment plan through one isolated-iteration callback."""

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Generic, Protocol, TypeVar, cast
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, model_validator

from app.domain.experiment.contracts import (
    AgentConfiguration,
    ConfigurationVariant,
    ExperimentContract,
    IterationIdentity,
    IterationPlan,
    PluginRef,
    ScenarioRef,
    SupportSuiteExperimentContract,
    build_iteration_plan,
)
from app.domain.experiment.errors import IterationExecutionError
from app.domain.experiment.events import ExperimentEvent, ExperimentEventKind
from app.domain.world.compiler import CompiledWorld

ResultT = TypeVar("ResultT")
ResultT_co = TypeVar("ResultT_co", covariant=True)
Clock = Callable[[], datetime]
EventSink = Callable[[ExperimentEvent], None]


class ExperimentExecutionContext(BaseModel):
    """The immutable inputs a runner must use for every iteration."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    contract: ExperimentContract | SupportSuiteExperimentContract
    world: CompiledWorld
    scenario: ScenarioRef | None = None
    plugin: PluginRef
    compiled_scenarios: tuple[object, ...] = ()
    plan: IterationPlan | None = None

    @model_validator(mode="after")
    def validate_identities(self) -> "ExperimentExecutionContext":
        """Bind the resolved inputs to exactly one contract and execution plan."""
        if self.world.identity != self.contract.world:
            raise ValueError("compiled world differs from the experiment contract")
        if self.plugin != self.contract.plugin:
            raise ValueError("resolved plugin differs from the experiment contract")
        expected_plan = build_iteration_plan(self.contract)

        if isinstance(self.contract, SupportSuiteExperimentContract):
            if self.scenario is not None:
                raise ValueError("v2 support-suite contexts must not select one default scenario")
            if self.plan != expected_plan:
                raise ValueError("v2 execution plan differs from the experiment contract")

            from app.domain.world.support import CompiledSupportScenario

            if any(
                not isinstance(compiled, CompiledSupportScenario)
                for compiled in self.compiled_scenarios
            ):
                raise ValueError("v2 contexts require compiled support scenarios")
            scenarios = cast(
                tuple[CompiledSupportScenario, ...],
                self.compiled_scenarios,
            )
            references = tuple(compiled.reference for compiled in scenarios)
            expected_references = tuple(
                member.scenario for member in self.contract.suite.members
            )
            if len(set(references)) != len(references) or set(references) != set(
                expected_references
            ):
                raise ValueError("compiled scenarios differ from the support suite members")
            if any(compiled.plugin_ref != self.contract.plugin for compiled in scenarios):
                raise ValueError("compiled scenario plugin differs from the experiment contract")
            return self

        if self.scenario != self.contract.scenario:
            raise ValueError("compiled scenario differs from the experiment contract")
        if self.compiled_scenarios:
            raise ValueError("v1 contexts must not carry support-suite scenarios")
        if self.plan is not None and self.plan != expected_plan:
            raise ValueError("execution plan differs from the experiment contract")
        return self


class IterationRunner(Protocol[ResultT_co]):
    """Execute one iteration in a fresh environment and return its evidence."""

    async def __call__(
        self,
        context: ExperimentExecutionContext,
        iteration: IterationIdentity,
        configuration: AgentConfiguration,
    ) -> ResultT_co: ...


@dataclass(frozen=True)
class CompletedIteration(Generic[ResultT]):
    """One iteration that returned evidence."""

    identity: IterationIdentity
    result: ResultT


@dataclass(frozen=True)
class FailedIteration:
    """One iteration that failed with a safe operational error code."""

    identity: IterationIdentity
    error_code: str


@dataclass(frozen=True)
class ExperimentExecution(Generic[ResultT]):
    """The complete local execution record for one deterministic plan."""

    execution_id: UUID
    contract_hash: str
    plan: IterationPlan
    iterations: tuple[CompletedIteration[ResultT] | FailedIteration, ...]

    @property
    def succeeded(self) -> bool:
        """Return true only when every scheduled iteration produced evidence."""
        return all(isinstance(item, CompletedIteration) for item in self.iterations)


async def execute_experiment(
    context: ExperimentExecutionContext,
    runner: IterationRunner[ResultT],
    *,
    execution_id: UUID | None = None,
    event_sink: EventSink | None = None,
    clock: Clock | None = None,
) -> ExperimentExecution[ResultT]:
    """Execute every scheduled iteration in order and retain each outcome.

    The callback owns environment creation and cleanup. Known operational
    failures use ``IterationExecutionError`` so the engine can continue the
    cohort without exposing an arbitrary exception message.
    """
    experiment = context.contract
    plan = context.plan or build_iteration_plan(experiment)
    attempt_id = execution_id or uuid4()
    now = clock or (lambda: datetime.now(UTC))
    sequence = 0

    def emit(
        kind: ExperimentEventKind,
        iteration: IterationIdentity | None = None,
    ) -> None:
        nonlocal sequence
        sequence += 1
        if event_sink is not None:
            event_sink(
                ExperimentEvent(
                    experiment_id=experiment.experiment_id,
                    execution_id=attempt_id,
                    sequence=sequence,
                    kind=kind,
                    emitted_at=now(),
                    iteration=iteration,
                )
            )

    emit(ExperimentEventKind.PLAN_CREATED)
    records: list[CompletedIteration[ResultT] | FailedIteration] = []
    for iteration in plan.iterations:
        emit(ExperimentEventKind.ITERATION_STARTED, iteration)
        configuration = _configuration_for(experiment, iteration.variant)
        try:
            async with asyncio.timeout(experiment.limits.max_duration_s):
                result = await runner(context, iteration, configuration)
        except TimeoutError:
            records.append(FailedIteration(identity=iteration, error_code="duration_limit_reached"))
            emit(ExperimentEventKind.ITERATION_FAILED, iteration)
        except IterationExecutionError as error:
            records.append(FailedIteration(identity=iteration, error_code=error.code))
            emit(ExperimentEventKind.ITERATION_FAILED, iteration)
        except Exception:
            records.append(
                FailedIteration(identity=iteration, error_code="unexpected_iteration_failure")
            )
            emit(ExperimentEventKind.ITERATION_FAILED, iteration)
        else:
            records.append(CompletedIteration(identity=iteration, result=result))
            emit(ExperimentEventKind.ITERATION_COMPLETED, iteration)

    terminal_kind = (
        ExperimentEventKind.EXPERIMENT_COMPLETED
        if all(isinstance(record, CompletedIteration) for record in records)
        else ExperimentEventKind.EXPERIMENT_FAILED
    )
    emit(terminal_kind)
    return ExperimentExecution(
        execution_id=attempt_id,
        contract_hash=experiment.content_hash,
        plan=plan,
        iterations=tuple(records),
    )


def _configuration_for(
    experiment: ExperimentContract | SupportSuiteExperimentContract,
    variant: ConfigurationVariant,
) -> AgentConfiguration:
    if variant is ConfigurationVariant.BASELINE:
        return experiment.baseline
    return experiment.candidate
