"""This module runs saved cases and suite comparisons in background tasks.

An execution handle tracks one background task, its live event collector,
its status, and its result. The live stream terminates cleanly when the
task completes. Failures are recorded on the handle as safe typed errors
instead of escaping the event loop.
"""

import asyncio
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from uuid import UUID, uuid4

from app.adapters.pydantic_ai_agent import ModelConfig
from app.domain.bundle.schemas import ConfigurationVersions
from app.domain.comparison.evaluators import ALL_EVALUATORS, Evaluator
from app.domain.comparison.experiment import ConfigurationChangeType
from app.domain.execution.errors import ExecutionNotFoundError
from app.domain.regression.schemas import RegressionCase
from app.domain.simulation.events import SimulationEvent, SimulationEventCollector
from app.domain.simulation.provisioner import ProvisionerFactory
from app.domain.simulation.runner import run_bundle
from app.domain.suite.runner import run_suite_comparison
from app.domain.suite.schemas import CaseSuite
from app.errors import DomainError

EXECUTION_STATUS_RUNNING = "running"
EXECUTION_STATUS_COMPLETED = "completed"
EXECUTION_STATUS_FAILED = "failed"


@dataclass
class ExecutionHandle:
    """This class tracks one background execution."""

    execution_id: UUID
    kind: str
    task: asyncio.Task[object]
    collector: SimulationEventCollector | None = None
    status: str = EXECUTION_STATUS_RUNNING
    result: object | None = None
    error_code: str | None = None
    error_message: str | None = None


class ExecutionService:
    """This class starts and tracks background case runs and suite comparisons."""

    def __init__(
        self,
        *,
        provisioner_factory: ProvisionerFactory,
        model_config: ModelConfig,
        evaluators: Sequence[Evaluator] = ALL_EVALUATORS,
    ) -> None:
        self._provisioner_factory = provisioner_factory
        self._model_config = model_config
        self._evaluators = tuple(evaluators)
        self._handles: dict[UUID, ExecutionHandle] = {}

    def start_run(self, *, case: RegressionCase) -> ExecutionHandle:
        """This method starts one saved case run in a background task."""
        execution_id = uuid4()
        collector = SimulationEventCollector()
        handle = ExecutionHandle(
            execution_id=execution_id,
            kind="run",
            task=asyncio.create_task(
                self._run_case_task(case, collector, execution_id)
            ),
            collector=collector,
        )
        self._handles[execution_id] = handle
        return handle

    def start_comparison(
        self,
        *,
        suite: CaseSuite,
        cases: Sequence[RegressionCase],
        change_type: ConfigurationChangeType,
        candidate: ConfigurationVersions,
        candidate_prompt: str | None = None,
        candidate_prompt_version: str | None = None,
    ) -> ExecutionHandle:
        """This method starts one suite comparison in a background task."""
        execution_id = uuid4()
        handle = ExecutionHandle(
            execution_id=execution_id,
            kind="comparison",
            task=asyncio.create_task(
                self._comparison_task(
                    suite,
                    tuple(cases),
                    change_type,
                    candidate,
                    candidate_prompt,
                    candidate_prompt_version,
                    execution_id,
                )
            ),
        )
        self._handles[execution_id] = handle
        return handle

    def get(self, execution_id: UUID) -> ExecutionHandle | None:
        """This method returns one execution handle, if it exists."""
        return self._handles.get(execution_id)

    def require(self, execution_id: UUID) -> ExecutionHandle:
        """This method returns one execution handle or raises a safe error."""
        handle = self._handles.get(execution_id)
        if handle is None:
            raise ExecutionNotFoundError(execution_id=execution_id)
        return handle

    async def _run_case_task(
        self,
        case: RegressionCase,
        collector: SimulationEventCollector,
        execution_id: UUID,
    ) -> None:
        try:
            result = await run_bundle(
                bundle=case.bundle,
                provisioner_factory=self._provisioner_factory,
                model_config=self._model_config,
                collector=collector,
                evaluators=self._evaluators,
            )
            self._finish(execution_id, result)
        except DomainError as error:
            self._fail(execution_id, error.code, error.message)
        except Exception as error:
            self._fail(execution_id, "execution_failed", str(error))

    async def _comparison_task(
        self,
        suite: CaseSuite,
        cases: tuple[RegressionCase, ...],
        change_type: ConfigurationChangeType,
        candidate: ConfigurationVersions,
        candidate_prompt: str | None,
        candidate_prompt_version: str | None,
        execution_id: UUID,
    ) -> None:
        try:
            result = await run_suite_comparison(
                suite=suite,
                cases=cases,
                change_type=change_type,
                candidate=candidate,
                provisioner_factory=self._provisioner_factory,
                baseline_model_config=self._model_config,
                candidate_prompt=candidate_prompt,
                candidate_prompt_version=candidate_prompt_version,
                evaluators=self._evaluators,
            )
            self._finish(execution_id, result)
        except DomainError as error:
            self._fail(execution_id, error.code, error.message)
        except Exception as error:
            self._fail(execution_id, "execution_failed", str(error))

    def _finish(self, execution_id: UUID, result: object) -> None:
        handle = self._handles.get(execution_id)
        if handle is None:
            return
        handle.status = EXECUTION_STATUS_COMPLETED
        handle.result = result

    def _fail(self, execution_id: UUID, code: str, message: str) -> None:
        handle = self._handles.get(execution_id)
        if handle is None:
            return
        handle.status = EXECUTION_STATUS_FAILED
        handle.error_code = code
        handle.error_message = message

    async def events(self, execution_id: UUID) -> AsyncIterator[SimulationEvent]:
        """This method streams one execution's live events and stops cleanly.

        The stream ends when the background task completes and the event
        queue is drained. A client that connects after completion receives
        the persisted transcript and then the clean end.
        """
        handle = self.require(execution_id)
        if handle.collector is None:
            raise ExecutionNotFoundError(execution_id=execution_id)
        if handle.task.done():
            for event in handle.collector.events():
                yield event
            return
        async for event in handle.collector.stream(until=handle.task.done):
            yield event
