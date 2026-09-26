"""Tests for deterministic local experiment execution."""

import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

from app.domain.experiment.contracts import (
    AgentConfiguration,
    ConfigurationVariant,
    ExperimentContract,
    IterationIdentity,
)
from app.domain.experiment.engine import (
    CompletedIteration,
    ExperimentExecutionContext,
    FailedIteration,
    execute_experiment,
)
from app.domain.experiment.errors import IterationExecutionError
from app.domain.experiment.events import ExperimentEvent, ExperimentEventKind
from app.domain.world.compiler import CompiledWorld

FIXTURES = Path(__file__).with_name("fixtures")
NOW = datetime(2026, 8, 30, tzinfo=UTC)
EXECUTION_ID = UUID("6ff6ed75-4f96-47b0-83c2-5c3768364104")


def support_experiment() -> ExperimentContract:
    payload = json.loads(
        (FIXTURES / "valid_support_experiment.json").read_text(encoding="utf-8")
    )
    return ExperimentContract.model_validate(payload)


def execution_context(experiment: ExperimentContract) -> ExperimentExecutionContext:
    return ExperimentExecutionContext(
        contract=experiment,
        world=CompiledWorld(identity=experiment.world, actors=()),
        scenario=experiment.scenario,
        plugin=experiment.plugin,
    )


async def test_engine_runs_reproducible_interleaved_plan_with_matching_configs() -> None:
    experiment = support_experiment()
    calls: list[tuple[IterationIdentity, AgentConfiguration]] = []
    events: list[ExperimentEvent] = []

    async def runner(
        context: ExperimentExecutionContext,
        iteration: IterationIdentity,
        configuration: AgentConfiguration,
    ) -> str:
        assert context.contract == experiment
        calls.append((iteration, configuration))
        return f"{iteration.variant.value}:{iteration.repetition}"

    execution = await execute_experiment(
        execution_context(experiment),
        runner,
        execution_id=EXECUTION_ID,
        event_sink=events.append,
        clock=lambda: NOW,
    )

    assert execution.succeeded
    assert execution.execution_id == EXECUTION_ID
    assert execution.contract_hash == experiment.content_hash
    assert [call[0] for call in calls] == list(execution.plan.iterations)
    assert len(execution.iterations) == experiment.interleaving.repetitions * 2
    for iteration, configuration in calls:
        expected = (
            experiment.baseline
            if iteration.variant is ConfigurationVariant.BASELINE
            else experiment.candidate
        )
        assert configuration == expected
    assert [event.sequence for event in events] == list(range(1, len(events) + 1))
    assert events[0].kind is ExperimentEventKind.PLAN_CREATED
    assert events[-1].kind is ExperimentEventKind.EXPERIMENT_COMPLETED


async def test_engine_retains_safe_failures_and_finishes_remaining_iterations() -> None:
    experiment = support_experiment()
    calls: list[int] = []
    events: list[ExperimentEvent] = []

    async def runner(
        context: ExperimentExecutionContext,
        iteration: IterationIdentity,
        configuration: AgentConfiguration,
    ) -> str:
        del context, configuration
        calls.append(iteration.ordinal)
        if iteration.ordinal == 2:
            raise IterationExecutionError("sandbox_start_failed")
        return "ok"

    execution = await execute_experiment(
        execution_context(experiment),
        runner,
        execution_id=EXECUTION_ID,
        event_sink=events.append,
        clock=lambda: NOW,
    )

    assert not execution.succeeded
    assert calls == list(range(1, len(execution.plan.iterations) + 1))
    assert isinstance(execution.iterations[0], CompletedIteration)
    failed = execution.iterations[1]
    assert isinstance(failed, FailedIteration)
    assert failed.error_code == "sandbox_start_failed"
    assert events[-1].kind is ExperimentEventKind.EXPERIMENT_FAILED
