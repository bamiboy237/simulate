"""Coordinate the bounded, synchronous support-experiment operator path.

The coordinator owns delivery-independent policy, short control-plane
transactions, and the in-process execution loop. It does not offer detached
execution or a way to interrupt an already-admitted model call.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from http import HTTPStatus
from secrets import compare_digest
from time import monotonic
from typing import NoReturn
from urllib.parse import urlsplit
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, SecretStr
from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import (
    AsyncConnection,
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from app.adapters.pydantic_ai_agent import ModelConfig
from app.config import Settings
from app.domain.agent.errors import ModelNotConfigured
from app.domain.comparison.evaluators import ALL_EVALUATORS
from app.domain.experiment.contracts import (
    BUILTIN_SUPPORT_RUNTIME_ARTIFACT_ID,
    BUILTIN_SUPPORT_RUNTIME_REFERENCE,
    AgentConfiguration,
    ArtifactKind,
    ArtifactRef,
    CandidateChangeType,
    EvaluatorRef,
    ExecutionLimits,
    ExperimentContract,
    InterleavingPlan,
    IterationIdentity,
    ModelRef,
    PluginRef,
    PromptRef,
    SupportSuiteExperimentContract,
    SupportSuiteMemberRef,
    SupportSuiteRef,
    build_iteration_plan,
    parse_experiment_contract,
)
from app.domain.experiment.engine import (
    CompletedIteration,
    ExperimentExecution,
    ExperimentExecutionContext,
    FailedIteration,
)
from app.domain.experiment.errors import ExperimentResultIntegrityError, IterationExecutionError
from app.domain.experiment.events import ExperimentEvent, ExperimentEventKind
from app.domain.experiment.models import ExperimentRecord
from app.domain.experiment.repository import SqlAlchemyExperimentRepository
from app.domain.experiment.service import ExperimentService
from app.domain.experiment.state_machine import (
    ExperimentConflictError,
    ExperimentNotFoundError,
    ExperimentPersistenceError,
    ExperimentStatus,
    InvalidExperimentTransitionError,
    TerminalExperimentImmutableError,
)
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
from app.domain.experiment.support_results import (
    SupportResult,
    assemble_support_experiment_result,
    assemble_support_suite_experiment_result,
    scrub_model_response,
)
from app.domain.regression.repository import SqlAlchemyRegressionCaseRepository
from app.domain.regression.service import RegressionCaseService
from app.domain.simulation.provisioner import ProvisionerFactory, postgres_provisioner_factory
from app.domain.simulation.runner import SimulationRun
from app.domain.suite.repository import SqlAlchemySuiteRepository
from app.domain.suite.schemas import suite_members_hash
from app.domain.suite.service import SuiteService
from app.domain.world.support import (
    SUPPORT_PLUGIN_VERSION,
    CompiledSupportScenario,
    SupportWorldPlugin,
)
from app.errors import DomainError

SessionFactory = Callable[[], AsyncSession]
_EXPERIMENT_ADMISSION_LOCK = 8_740_219
_EXPERIMENT_OPERATOR_LOCK = 8_740_220
_ENDPOINT_FINGERPRINT_PREFIX = "endpoint-"
_DEFAULT_MODEL_ENDPOINTS = {
    "anthropic": "https://api.anthropic.com",
    "openai": "https://api.openai.com/v1",
}
_DATABASE_IDENTITY_QUERY = text(
    "SELECT current_database() AS database_name, "
    "(pg_control_system()).system_identifier::text AS system_identifier"
)
_OPERATOR_LOCK_TIMEOUT_QUERY = text(
    "SELECT setting::double precision / 1000 "
    "FROM pg_settings WHERE name = 'idle_in_transaction_session_timeout'"
)


class SupportPromptReference(BaseModel):
    """An immutable identity for a prompt whose body is supplied only at start."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    prompt_id: str = Field(pattern=r"^[a-z][a-z0-9_.-]*$", max_length=200)
    version: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._+-]*$", max_length=100)
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")

    def as_prompt_ref(self) -> PromptRef:
        return PromptRef(**self.model_dump())


class SupportPromptRuntime(BaseModel):
    """A write-only prompt body that must reproduce a persisted prompt identity."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    prompt_id: str = Field(pattern=r"^[a-z][a-z0-9_.-]*$", max_length=200)
    version: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._+-]*$", max_length=100)
    content: SecretStr

    def as_reference(self) -> SupportPromptReference:
        body = self.content.get_secret_value()
        if not body.strip():
            _operator_error(
                "experiment_prompt_invalid",
                "A support experiment prompt must not be empty",
                HTTPStatus.UNPROCESSABLE_ENTITY,
            )
        return SupportPromptReference(
            prompt_id=self.prompt_id,
            version=self.version,
            content_hash=sha256(body.encode("utf-8")).hexdigest(),
        )


class SupportExperimentDraft(BaseModel):
    """The operator inputs needed to construct one support-only experiment."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    case_id: UUID
    case_version: int = Field(ge=1)
    candidate_change: CandidateChangeType
    baseline_prompt: SupportPromptReference
    candidate_prompt: SupportPromptReference
    limits: ExecutionLimits
    repetitions: int = Field(ge=3, le=10_000)
    seed: int


class SupportSuiteExperimentDraft(BaseModel):
    """The operator inputs needed to construct one saved-support-suite experiment."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    suite_id: UUID
    suite_version: int = Field(ge=1)
    candidate_change: CandidateChangeType
    baseline_prompt: SupportPromptReference
    candidate_prompt: SupportPromptReference
    limits: ExecutionLimits
    repetitions: int = Field(ge=3, le=10_000)
    seed: int


class SupportExperimentStartInputs(BaseModel):
    """Ephemeral prompt bodies used to execute a previously created experiment."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    baseline_prompt: SupportPromptRuntime
    candidate_prompt: SupportPromptRuntime | None = None


class ExperimentOperatorError(DomainError):
    """A stable, delivery-safe failure from the support experiment coordinator."""


def authorize_support_experiment_operator(
    settings: Settings,
    operator_credential: SecretStr | None,
) -> SecretStr:
    """Verify the configured single-operator credential before model work begins."""
    configured_credential = settings.experiment_operator_token
    if configured_credential is None or not configured_credential.get_secret_value():
        _operator_error(
            "experiment_operator_not_configured",
            "Experiment execution is disabled until an operator credential is configured",
            HTTPStatus.SERVICE_UNAVAILABLE,
        )
    if operator_credential is None or not compare_digest(
        operator_credential.get_secret_value().encode("utf-8"),
        configured_credential.get_secret_value().encode("utf-8"),
    ):
        _operator_error(
            "experiment_operator_unauthorized",
            "A valid experiment operator credential is required",
            HTTPStatus.UNAUTHORIZED,
        )
    return operator_credential


@dataclass(frozen=True)
class _PreparedExecution:
    """Validated inputs retained locally while the synchronous execution runs."""

    contract: ExperimentContract | SupportSuiteExperimentContract
    context: ExperimentExecutionContext
    scenarios: tuple[CompiledSupportScenario, ...]
    runner: SupportIterationRunner
    sandbox_engine: AsyncEngine


@dataclass(frozen=True)
class _DatabaseIdentity:
    """The server-observed identity needed to distinguish PostgreSQL databases."""

    system_identifier: str
    database_name: str


class _ExperimentDeadlineReached(Exception):
    """Internal signal that a committed total-duration failure must be terminal."""


class SupportExperimentOperator:
    """Run one bounded support experiment with committed incremental evidence.

    This is a single-operator control-plane capability. It has no project or
    tenant identity model; the delivery layer protects it with one configured
    operator credential.
    """

    def __init__(
        self,
        *,
        session_factory: SessionFactory,
        settings: Settings,
        provisioner_factory: ProvisionerFactory | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._settings = settings
        self._provisioner_factory = provisioner_factory

    async def create(
        self,
        draft: SupportExperimentDraft | SupportSuiteExperimentDraft,
    ) -> ExperimentRecord:
        """Persist a v1 case or v2 saved-suite contract with immutable identities."""
        self._validate_repetition_cap(draft.repetitions)
        async with self._session_factory() as session:
            plugin_ref = self._current_plugin_ref()
            try:
                plugin = SupportWorldPlugin(plugin_ref)
            except ValueError:
                _operator_error(
                    "experiment_plugin_unavailable",
                    "The approved support world cannot compile in this operator",
                    HTTPStatus.SERVICE_UNAVAILABLE,
                )

            baseline_model, baseline_runtime = self._baseline_model()
            candidate_model, _ = self._candidate_model(
                draft.candidate_change,
                baseline_model=baseline_model,
                baseline_runtime=baseline_runtime,
            )
            artifact = self._current_runtime_artifact()

            try:
                if isinstance(draft, SupportExperimentDraft):
                    cases = RegressionCaseService(SqlAlchemyRegressionCaseRepository(session))
                    case = await cases.get_case(
                        case_id=draft.case_id,
                        case_version=draft.case_version,
                    )
                    try:
                        scenario = plugin.compile_case(case)
                    except ValueError:
                        _operator_error(
                            "experiment_case_not_executable",
                            "The selected case cannot compile into the approved support world",
                            HTTPStatus.UNPROCESSABLE_ENTITY,
                        )
                    contract: ExperimentContract | SupportSuiteExperimentContract = (
                        ExperimentContract(
                            experiment_id=uuid4(),
                            world=plugin.compiled_world.identity,
                            scenario=scenario.reference,
                            plugin=plugin_ref,
                            evaluators=self._current_evaluators(),
                            baseline=AgentConfiguration(
                                artifact=artifact,
                                model=baseline_model,
                                prompt=draft.baseline_prompt.as_prompt_ref(),
                            ),
                            candidate=AgentConfiguration(
                                artifact=artifact,
                                model=candidate_model,
                                prompt=draft.candidate_prompt.as_prompt_ref(),
                            ),
                            candidate_change=draft.candidate_change,
                            limits=draft.limits,
                            interleaving=InterleavingPlan(
                                repetitions=draft.repetitions,
                                seed=draft.seed,
                            ),
                        )
                    )
                else:
                    suite, _ = await self._resolve_compiled_suite(
                        session,
                        suite_id=draft.suite_id,
                        suite_version=draft.suite_version,
                        plugin=plugin,
                    )
                    contract = SupportSuiteExperimentContract(
                        experiment_id=uuid4(),
                        world=plugin.compiled_world.identity,
                        suite=suite,
                        plugin=plugin_ref,
                        evaluators=self._current_evaluators(),
                        baseline=AgentConfiguration(
                            artifact=artifact,
                            model=baseline_model,
                            prompt=draft.baseline_prompt.as_prompt_ref(),
                        ),
                        candidate=AgentConfiguration(
                            artifact=artifact,
                            model=candidate_model,
                            prompt=draft.candidate_prompt.as_prompt_ref(),
                        ),
                        candidate_change=draft.candidate_change,
                        limits=draft.limits,
                        interleaving=InterleavingPlan(
                            repetitions=draft.repetitions,
                            seed=draft.seed,
                        ),
                    )
            except ValueError:
                _operator_error(
                    "experiment_draft_invalid",
                    "The candidate must differ only in its declared model or prompt",
                    HTTPStatus.UNPROCESSABLE_ENTITY,
                )
            service = ExperimentService(SqlAlchemyExperimentRepository(session))
            record = await service.create(contract)
            await session.commit()
            return record

    async def start(
        self,
        experiment_id: UUID,
        inputs: SupportExperimentStartInputs,
        *,
        operator_credential: SecretStr | None,
    ) -> ExperimentRecord:
        """Run a preflighted experiment and commit lifecycle evidence incrementally."""
        authorize_support_experiment_operator(self._settings, operator_credential)
        self._require_single_operator_configuration()
        prepared = await self._prepare_execution(experiment_id, inputs)
        try:
            async with self._exclusive_operator_lock():
                await self._reconcile_orphaned_after_lock()
                return await self._run_prepared_execution(experiment_id, prepared)
        finally:
            await prepared.sandbox_engine.dispose()

    async def _run_prepared_execution(
        self,
        experiment_id: UUID,
        prepared: _PreparedExecution,
    ) -> ExperimentRecord:
        """Execute after the global operator lock makes orphan reconciliation safe."""
        execution_id = uuid4()
        try:
            await self._admit_execution(experiment_id, execution_id)
            execution = await self._execute_incrementally(
                prepared.context,
                prepared.runner,
                execution_id=execution_id,
            )
            if not execution.succeeded:
                await self._fail_execution(
                    experiment_id,
                    execution_id,
                    error_code="iteration_execution_failed",
                )
                return await self.get_record(experiment_id)

            try:
                if isinstance(prepared.contract, SupportSuiteExperimentContract):
                    result: SupportResult = assemble_support_suite_experiment_result(
                        prepared.contract,
                        execution,
                        scenarios=prepared.scenarios,
                    )
                else:
                    result = assemble_support_experiment_result(
                        prepared.contract,
                        execution,
                        scenario=prepared.scenarios[0],
                    )
            except ExperimentResultIntegrityError:
                await self._fail_execution(
                    experiment_id,
                    execution_id,
                    error_code="result_integrity_failed",
                )
                return await self.get_record(experiment_id)

            completion_event = ExperimentEvent(
                experiment_id=experiment_id,
                execution_id=execution_id,
                sequence=(2 * len(execution.plan.iterations)) + 2,
                kind=ExperimentEventKind.EXPERIMENT_COMPLETED,
                emitted_at=datetime.now(UTC),
            )
            await self._publish_result(experiment_id, execution_id, result, completion_event)
            return await self.get_record(experiment_id)
        except _ExperimentDeadlineReached:
            await self._fail_execution(
                experiment_id,
                execution_id,
                error_code="total_duration_limit_reached",
            )
            return await self.get_record(experiment_id)
        except asyncio.CancelledError:
            await self._attempt_failure(
                experiment_id,
                execution_id,
                error_code="operator_request_cancelled",
            )
            raise
        except ExperimentOperatorError:
            await self._attempt_failure(
                experiment_id,
                execution_id,
                error_code="operator_execution_failed",
            )
            raise
        except Exception:
            await self._attempt_failure(
                experiment_id,
                execution_id,
                error_code="operator_execution_failed",
            )
            return await self.get_record(experiment_id)

    async def get_record(self, experiment_id: UUID) -> ExperimentRecord:
        """Return one persisted experiment without exposing prompt bodies."""
        async with self._session_factory() as session:
            record = await SqlAlchemyExperimentRepository(session).get_by_id(experiment_id)
        if record is None:
            _operator_error(
                "experiment_not_found",
                "The requested experiment does not exist",
                HTTPStatus.NOT_FOUND,
            )
        return record

    async def replay(
        self,
        experiment_id: UUID,
        *,
        after_sequence: int,
        limit: int,
    ) -> list[ExperimentEvent]:
        """Return durable events in their original sequence order."""
        record = await self.get_record(experiment_id)
        if record.execution_id is None:
            _operator_error(
                "experiment_not_started",
                "The experiment has not started and has no event stream",
                HTTPStatus.CONFLICT,
            )
        async with self._session_factory() as session:
            service = ExperimentService(SqlAlchemyExperimentRepository(session))
            persisted = await service.replay_events(
                experiment_id,
                record.execution_id,
                after_sequence=after_sequence,
                limit=limit,
            )
        return [ExperimentEvent.model_validate(event.payload) for event in persisted]

    async def cancel(self, experiment_id: UUID) -> ExperimentRecord:
        """Cancel only a pending experiment."""
        record = await self.get_record(experiment_id)
        if record.status != ExperimentStatus.PENDING.value:
            _operator_error(
                "experiment_not_cancellable",
                "Only a pending experiment can be cancelled",
                HTTPStatus.CONFLICT,
            )
        try:
            async with self._session_factory() as session:
                service = ExperimentService(SqlAlchemyExperimentRepository(session))
                cancelled = await service.cancel(experiment_id, record.execution_id)
                await session.commit()
                return cancelled
        except (
            ExperimentConflictError,
            InvalidExperimentTransitionError,
            TerminalExperimentImmutableError,
        ):
            _operator_error(
                "experiment_not_cancellable",
                "Only a pending experiment can be cancelled",
                HTTPStatus.CONFLICT,
            )

    async def verified_result(self, experiment_id: UUID) -> SupportResult:
        """Load a result only after lifecycle and content verification."""
        try:
            async with self._session_factory() as session:
                result = await ExperimentService(
                    SqlAlchemyExperimentRepository(session)
                ).get_result(experiment_id)
        except ExperimentNotFoundError:
            _operator_error(
                "experiment_not_found",
                "The requested experiment does not exist",
                HTTPStatus.NOT_FOUND,
            )
        except ExperimentPersistenceError:
            _operator_error(
                "experiment_result_invalid",
                "The persisted experiment result is not valid",
                HTTPStatus.CONFLICT,
            )
        if result is None:
            _operator_error(
                "experiment_result_unavailable",
                "A verified result is available only after a completed experiment",
                HTTPStatus.CONFLICT,
            )
        return result

    def _require_single_operator_configuration(self) -> None:
        """Reject concurrent operation until execution has durable ownership leases."""
        if self._settings.experiment_max_concurrency != 1:
            _operator_error(
                "experiment_single_operator_required",
                "Synchronous support experiments require exactly one active operator",
                HTTPStatus.SERVICE_UNAVAILABLE,
            )

    @asynccontextmanager
    async def _exclusive_operator_lock(self) -> AsyncIterator[None]:
        """Hold one PostgreSQL transaction advisory lock through synchronous execution."""
        database_url = self._settings.database_url_unpooled or self._settings.database_url
        engine = create_async_engine(str(database_url), poolclass=NullPool)
        try:
            async with engine.connect() as connection:
                transaction = await connection.begin()
                try:
                    idle_timeout_s = await connection.scalar(_OPERATOR_LOCK_TIMEOUT_QUERY)
                    self._require_operator_lock_timeout(idle_timeout_s)
                    acquired = await connection.scalar(
                        text("SELECT pg_try_advisory_xact_lock(:lock_key)"),
                        {"lock_key": _EXPERIMENT_OPERATOR_LOCK},
                    )
                    if acquired is not True:
                        _operator_error(
                            "experiment_operator_busy",
                            "Another worker is already executing a synchronous support experiment",
                            HTTPStatus.SERVICE_UNAVAILABLE,
                        )
                    yield
                finally:
                    await transaction.rollback()
        except ExperimentOperatorError:
            raise
        except Exception:
            _operator_error(
                "experiment_operator_unavailable",
                "The experiment operator lock is unavailable",
                HTTPStatus.SERVICE_UNAVAILABLE,
            )
        finally:
            await engine.dispose()

    def _require_operator_lock_timeout(self, configured_timeout: object) -> None:
        """Reject a server timeout that could release the lock during a valid run."""
        if not isinstance(configured_timeout, (float, int, str)):
            _operator_error(
                "experiment_operator_lock_unverifiable",
                "The experiment operator lock timeout cannot be verified",
                HTTPStatus.SERVICE_UNAVAILABLE,
            )
        try:
            timeout_s = float(configured_timeout)
        except (TypeError, ValueError):
            _operator_error(
                "experiment_operator_lock_unverifiable",
                "The experiment operator lock timeout cannot be verified",
                HTTPStatus.SERVICE_UNAVAILABLE,
            )
        if 0 < timeout_s <= self._settings.experiment_max_total_duration_s:
            _operator_error(
                "experiment_operator_lock_timeout_unsafe",
                "The database idle transaction timeout is too short for this experiment",
                HTTPStatus.SERVICE_UNAVAILABLE,
            )

    async def _reconcile_orphaned_after_lock(self) -> int:
        """Fail stale runs only after the exclusive operator lock proves no worker owns them."""
        async with self._session_factory() as session:
            orphaned_count = await session.scalar(
                select(func.count())
                .select_from(ExperimentRecord)
                .where(ExperimentRecord.status == ExperimentStatus.RUNNING.value)
            )
            await session.execute(
                update(ExperimentRecord)
                .where(ExperimentRecord.status == ExperimentStatus.RUNNING.value)
                .values(
                    status=ExperimentStatus.FAILED.value,
                    error_code="operator_process_restarted",
                    finished_at=datetime.now(UTC),
                )
            )
            await session.commit()
            return int(orphaned_count or 0)

    async def _prepare_execution(
        self,
        experiment_id: UUID,
        inputs: SupportExperimentStartInputs,
    ) -> _PreparedExecution:
        record = await self.get_record(experiment_id)
        if record.status != ExperimentStatus.PENDING.value:
            _operator_error(
                "experiment_not_pending",
                "Only a pending experiment can start",
                HTTPStatus.CONFLICT,
            )
        contract = self._stored_contract(record)
        self._validate_execution_caps(contract)
        sandbox_engine = await self._verify_sandbox_preflight()
        try:
            plugin = self._current_plugin_for(contract)
            if isinstance(contract, SupportSuiteExperimentContract):
                scenarios = await self._compiled_suite_scenarios(contract, plugin)
                context = ExperimentExecutionContext(
                    contract=contract,
                    world=plugin.compiled_world,
                    plugin=plugin.plugin_ref,
                    compiled_scenarios=scenarios,
                    plan=build_iteration_plan(contract),
                )
            else:
                scenario = await self._compiled_scenario(contract)
                scenarios = (scenario,)
                context = ExperimentExecutionContext(
                    contract=contract,
                    world=plugin.compiled_world,
                    scenario=scenario.reference,
                    plugin=plugin.plugin_ref,
                )
            runtime = self._runtime_bindings(contract, inputs)
            provisioner_factory = self._support_provisioner_factory(sandbox_engine)
            runner = SupportIterationRunner(
                compiled_world=plugin.compiled_world,
                scenario=None
                if isinstance(contract, SupportSuiteExperimentContract)
                else scenarios[0],
                plugin=plugin,
                provisioner_factory=provisioner_factory,
                runtime=runtime,
                evaluators=tuple(
                    PinnedSupportEvaluator(reference=reference, evaluator=evaluator)
                    for reference, evaluator in zip(
                        contract.evaluators,
                        ALL_EVALUATORS,
                        strict=True,
                    )
                ),
            )
            return _PreparedExecution(
                contract=contract,
                context=context,
                scenarios=scenarios,
                runner=runner,
                sandbox_engine=sandbox_engine,
            )
        except Exception:
            await sandbox_engine.dispose()
            raise

    async def _admit_execution(self, experiment_id: UUID, execution_id: UUID) -> None:
        """Serialize starts so the configured global model-call cap is real."""
        try:
            async with self._session_factory() as session:
                await session.execute(
                    text("SELECT pg_advisory_xact_lock(:lock_key)"),
                    {"lock_key": _EXPERIMENT_ADMISSION_LOCK},
                )
                repository = SqlAlchemyExperimentRepository(session)
                record = await repository.get_by_id(experiment_id)
                if (
                    record is None
                    or record.status != ExperimentStatus.PENDING.value
                    or record.execution_id is not None
                ):
                    raise ExperimentConflictError(
                        "experiment lifecycle changed before execution could start"
                    )
                running = await session.scalar(
                    select(func.count())
                    .select_from(ExperimentRecord)
                    .where(ExperimentRecord.status == ExperimentStatus.RUNNING.value)
                )
                if (running or 0) >= self._settings.experiment_max_concurrency:
                    _operator_error(
                        "experiment_concurrency_limit_reached",
                        "The configured experiment concurrency limit is already in use",
                        HTTPStatus.TOO_MANY_REQUESTS,
                    )
                service = ExperimentService(repository)
                await service.start_execution(experiment_id, execution_id)
                await session.commit()
        except (
            ExperimentConflictError,
            InvalidExperimentTransitionError,
            TerminalExperimentImmutableError,
        ):
            _operator_error(
                "experiment_not_pending",
                "Only a pending experiment can start",
                HTTPStatus.CONFLICT,
            )

    async def _execute_incrementally(
        self,
        context: ExperimentExecutionContext,
        runner: SupportIterationRunner,
        *,
        execution_id: UUID,
    ) -> ExperimentExecution[SimulationRun]:
        """Persist each boundary before allowing the next model call to begin."""
        contract = context.contract
        plan = build_iteration_plan(contract)
        sequence = 0

        async def persist_event(
            kind: ExperimentEventKind,
            iteration: IterationIdentity | None = None,
        ) -> ExperimentEvent:
            nonlocal sequence
            sequence += 1
            event = ExperimentEvent(
                experiment_id=contract.experiment_id,
                execution_id=execution_id,
                sequence=sequence,
                kind=kind,
                emitted_at=datetime.now(UTC),
                iteration=iteration,
            )
            await self._record_events(contract.experiment_id, execution_id, [event])
            return event

        await persist_event(ExperimentEventKind.PLAN_CREATED)
        records: list[CompletedIteration[SimulationRun] | FailedIteration] = []
        deadline = monotonic() + self._settings.experiment_max_total_duration_s

        for iteration in plan.iterations:
            if monotonic() >= deadline:
                await persist_event(ExperimentEventKind.LIMIT_REACHED)
                await persist_event(ExperimentEventKind.EXPERIMENT_FAILED)
                raise _ExperimentDeadlineReached

            await persist_event(ExperimentEventKind.ITERATION_STARTED, iteration)
            configuration = (
                contract.baseline
                if iteration.variant.value == "baseline"
                else contract.candidate
            )
            remaining_s = deadline - monotonic()
            outcome: CompletedIteration[SimulationRun] | FailedIteration
            try:
                async with asyncio.timeout(min(contract.limits.max_duration_s, remaining_s)):
                    result = await runner(context, iteration, configuration)
            except TimeoutError:
                error_code = (
                    "total_duration_limit_reached"
                    if monotonic() >= deadline
                    else "duration_limit_reached"
                )
                outcome = FailedIteration(identity=iteration, error_code=error_code)
            except IterationExecutionError as error:
                outcome = FailedIteration(identity=iteration, error_code=error.code)
            except Exception:
                outcome = FailedIteration(
                    identity=iteration,
                    error_code="unexpected_iteration_failure",
                )
            else:
                outcome = CompletedIteration(
                    identity=iteration,
                    result=scrub_model_response(result),
                )

            records.append(outcome)
            terminal_kind = (
                ExperimentEventKind.ITERATION_COMPLETED
                if isinstance(outcome, CompletedIteration)
                else ExperimentEventKind.ITERATION_FAILED
            )
            await self._record_iteration(
                contract.experiment_id,
                execution_id,
                outcome,
                ExperimentEvent(
                    experiment_id=contract.experiment_id,
                    execution_id=execution_id,
                    sequence=sequence + 1,
                    kind=terminal_kind,
                    emitted_at=datetime.now(UTC),
                    iteration=iteration,
                ),
            )
            sequence += 1

            if (
                isinstance(outcome, FailedIteration)
                and outcome.error_code == "total_duration_limit_reached"
            ):
                await persist_event(ExperimentEventKind.LIMIT_REACHED)
                await persist_event(ExperimentEventKind.EXPERIMENT_FAILED)
                raise _ExperimentDeadlineReached

        if not all(isinstance(record, CompletedIteration) for record in records):
            await persist_event(ExperimentEventKind.EXPERIMENT_FAILED)
        return ExperimentExecution(
            execution_id=execution_id,
            contract_hash=contract.content_hash,
            plan=plan,
            iterations=tuple(records),
        )

    async def _record_events(
        self,
        experiment_id: UUID,
        execution_id: UUID,
        events: list[ExperimentEvent],
    ) -> None:
        async with self._session_factory() as session:
            service = ExperimentService(SqlAlchemyExperimentRepository(session))
            await service.record_events(experiment_id, execution_id, events)
            await session.commit()

    async def _record_iteration(
        self,
        experiment_id: UUID,
        execution_id: UUID,
        outcome: CompletedIteration[SimulationRun] | FailedIteration,
        event: ExperimentEvent,
    ) -> None:
        async with self._session_factory() as session:
            service = ExperimentService(SqlAlchemyExperimentRepository(session))
            await service.record_iteration_outcome(experiment_id, execution_id, outcome)
            await service.record_events(experiment_id, execution_id, [event])
            await session.commit()

    async def _publish_result(
        self,
        experiment_id: UUID,
        execution_id: UUID,
        result: SupportResult,
        completion_event: ExperimentEvent,
    ) -> None:
        async with self._session_factory() as session:
            service = ExperimentService(SqlAlchemyExperimentRepository(session))
            await service.publish_result(
                experiment_id,
                execution_id,
                result,
                completion_event=completion_event,
            )
            await session.commit()

    async def _fail_execution(
        self,
        experiment_id: UUID,
        execution_id: UUID,
        *,
        error_code: str,
    ) -> None:
        async with self._session_factory() as session:
            service = ExperimentService(SqlAlchemyExperimentRepository(session))
            await service.fail(experiment_id, execution_id, error_code=error_code)
            await session.commit()

    async def _attempt_failure(
        self,
        experiment_id: UUID,
        execution_id: UUID,
        *,
        error_code: str,
    ) -> None:
        try:
            await self._fail_execution(experiment_id, execution_id, error_code=error_code)
        except Exception:
            return

    def _validate_execution_caps(
        self,
        contract: ExperimentContract | SupportSuiteExperimentContract,
    ) -> None:
        """Reject limits that exceed server policy or cannot preempt model spend."""
        self._validate_repetition_cap(contract.interleaving.repetitions)
        plan = build_iteration_plan(contract)
        if isinstance(contract, SupportSuiteExperimentContract) and len(plan.iterations) < 18:
            _operator_error(
                "experiment_suite_iterations_too_few",
                "A support-suite experiment requires at least 18 planned iterations",
                HTTPStatus.UNPROCESSABLE_ENTITY,
            )
        if contract.limits.max_duration_s > self._settings.experiment_max_iteration_duration_s:
            _operator_error(
                "experiment_iteration_duration_exceeds_cap",
                "The requested per-iteration duration exceeds the operator cap",
                HTTPStatus.UNPROCESSABLE_ENTITY,
            )
        planned_duration_s = len(plan.iterations) * contract.limits.max_duration_s
        if planned_duration_s > self._settings.experiment_max_total_duration_s:
            _operator_error(
                "experiment_total_duration_exceeds_cap",
                "The requested total duration exceeds the operator cap",
                HTTPStatus.UNPROCESSABLE_ENTITY,
            )
        if contract.limits.max_tokens is not None or contract.limits.max_cost_usd is not None:
            _operator_error(
                "experiment_budget_unenforceable",
                "This runner cannot preempt token or cost spending",
                HTTPStatus.UNPROCESSABLE_ENTITY,
            )
        if contract.limits.max_tool_calls is not None or contract.limits.max_retries is not None:
            _operator_error(
                "experiment_limit_unenforceable",
                "This runner cannot preempt tool-call or retry limits",
                HTTPStatus.UNPROCESSABLE_ENTITY,
            )
        if contract.limits.max_turns < 1:
            _operator_error(
                "experiment_turn_limit_invalid",
                "The support runner requires at least one allowed turn",
                HTTPStatus.UNPROCESSABLE_ENTITY,
            )

    def _validate_repetition_cap(self, repetitions: int) -> None:
        if repetitions > self._settings.experiment_max_repetitions:
            _operator_error(
                "experiment_repetitions_exceed_cap",
                "The requested repetitions exceed the operator cap",
                HTTPStatus.UNPROCESSABLE_ENTITY,
            )

    async def _verify_sandbox_preflight(self) -> AsyncEngine:
        """Verify a distinct disposable sandbox before changing lifecycle state."""
        sandbox_url = self._settings.experiment_sandbox_database_url
        if sandbox_url is None or not self._settings.experiment_sandbox_disposable:
            _operator_error(
                "experiment_sandbox_unavailable",
                "A separately configured disposable experiment sandbox is required",
                HTTPStatus.SERVICE_UNAVAILABLE,
            )
        engine: AsyncEngine | None = None
        try:
            engine = create_async_engine(str(sandbox_url), poolclass=NullPool)
            async with engine.connect() as connection:
                sandbox_identity = await _observed_database_identity(connection)
                for control_plane_url in (
                    self._settings.database_url,
                    self._settings.database_url_unpooled,
                ):
                    if control_plane_url is None:
                        continue
                    control_identity = await _database_identity_for_url(str(control_plane_url))
                    if sandbox_identity == control_identity:
                        _operator_error(
                            "experiment_sandbox_not_isolated",
                            "The experiment sandbox must not be the control-plane database",
                            HTTPStatus.UNPROCESSABLE_ENTITY,
                        )
                await connection.execute(text("SELECT 1"))
                await connection.execute(
                    text(
                        "CREATE TEMPORARY TABLE experiment_sandbox_preflight "
                        "(id integer) ON COMMIT DROP"
                    )
                )
                await connection.rollback()
        except ExperimentOperatorError:
            if engine is not None:
                await engine.dispose()
            raise
        except Exception:
            if engine is not None:
                await engine.dispose()
            _operator_error(
                "experiment_sandbox_unavailable",
                "The configured experiment sandbox cannot prove separate disposable isolation",
                HTTPStatus.SERVICE_UNAVAILABLE,
            )
        if engine is None:
            raise AssertionError("sandbox preflight did not create an engine")
        return engine

    def _support_provisioner_factory(self, sandbox_engine: AsyncEngine) -> ProvisionerFactory:
        if self._provisioner_factory is not None:
            return self._provisioner_factory
        sandbox_url = self._settings.experiment_sandbox_database_url
        if sandbox_url is None:
            _operator_error(
                "experiment_sandbox_unavailable",
                "A separately configured disposable experiment sandbox is required",
                HTTPStatus.SERVICE_UNAVAILABLE,
            )
        session_factory = async_sessionmaker(sandbox_engine, expire_on_commit=False)
        return postgres_provisioner_factory(
            session_factory,
            database_url=str(sandbox_url),
            environment="test",
            isolation_confirmed=True,
        )

    def _current_plugin_ref(self) -> PluginRef:
        try:
            content_hash = builtin_support_plugin_content_hash()
        except Exception:
            _operator_error(
                "experiment_runtime_attestation_unavailable",
                "The built-in support plugin source cannot be attested",
                HTTPStatus.SERVICE_UNAVAILABLE,
            )
        return PluginRef(
            plugin_id="support",
            version=SUPPORT_PLUGIN_VERSION,
            content_hash=content_hash,
        )

    def _current_plugin_for(
        self,
        contract: ExperimentContract | SupportSuiteExperimentContract,
    ) -> SupportWorldPlugin:
        plugin_ref = self._current_plugin_ref()
        if contract.plugin != plugin_ref:
            _operator_error(
                "experiment_plugin_identity_changed",
                "The current support plugin no longer matches the persisted experiment",
                HTTPStatus.CONFLICT,
            )
        return SupportWorldPlugin(plugin_ref)

    def _current_runtime_artifact(self) -> ArtifactRef:
        try:
            content_hash = builtin_support_runtime_content_hash()
        except Exception:
            _operator_error(
                "experiment_runtime_attestation_unavailable",
                "The built-in support runtime source cannot be attested",
                HTTPStatus.SERVICE_UNAVAILABLE,
            )
        return ArtifactRef(
            artifact_id=BUILTIN_SUPPORT_RUNTIME_ARTIFACT_ID,
            kind=ArtifactKind.BUILTIN_SUPPORT_RUNTIME,
            reference=BUILTIN_SUPPORT_RUNTIME_REFERENCE,
            content_hash=content_hash,
        )

    def _current_evaluators(self) -> tuple[EvaluatorRef, ...]:
        try:
            content_hash = builtin_support_evaluator_content_hash()
        except Exception:
            _operator_error(
                "experiment_runtime_attestation_unavailable",
                "The built-in evaluator source cannot be attested",
                HTTPStatus.SERVICE_UNAVAILABLE,
            )
        return tuple(
            EvaluatorRef(
                evaluator_id=evaluator.name,
                version=evaluator.version,
                content_hash=content_hash,
            )
            for evaluator in ALL_EVALUATORS
        )

    def _baseline_model(self) -> tuple[ModelRef, ModelConfig]:
        if not self._settings.model_configured:
            raise ModelNotConfigured()
        provider = self._settings.model_provider
        name = self._settings.model_name
        if provider is None or name is None:
            raise ModelNotConfigured()
        runtime = ModelConfig(
            provider=provider,
            name=name,
            base_url=self._settings.model_base_url,
            api_key=self._settings.model_api_key,
        )
        return self._model_ref(runtime), runtime

    def _candidate_model(
        self,
        candidate_change: CandidateChangeType,
        *,
        baseline_model: ModelRef,
        baseline_runtime: ModelConfig,
    ) -> tuple[ModelRef, ModelConfig]:
        if candidate_change is CandidateChangeType.PROMPT:
            return baseline_model, baseline_runtime
        if not self._settings.candidate_model_configured:
            _operator_error(
                "candidate_model_not_configured",
                "A model candidate requires MODEL_CANDIDATE_PROVIDER and MODEL_CANDIDATE_NAME",
                HTTPStatus.SERVICE_UNAVAILABLE,
            )
        provider = self._settings.model_candidate_provider
        name = self._settings.model_candidate_name
        if provider is None or name is None:
            _operator_error(
                "candidate_model_not_configured",
                "A model candidate requires MODEL_CANDIDATE_PROVIDER and MODEL_CANDIDATE_NAME",
                HTTPStatus.SERVICE_UNAVAILABLE,
            )
        runtime = ModelConfig(
            provider=provider,
            name=name,
            base_url=self._settings.model_candidate_base_url,
            api_key=self._settings.model_candidate_api_key,
        )
        return self._model_ref(runtime), runtime

    @staticmethod
    def _model_ref(runtime: ModelConfig) -> ModelRef:
        try:
            endpoint = _normalized_model_endpoint(runtime)
            return ModelRef(
                provider=runtime.provider,
                model_id=runtime.name,
                version=f"{_ENDPOINT_FINGERPRINT_PREFIX}{sha256(endpoint.encode()).hexdigest()}",
            )
        except ValueError:
            _operator_error(
                "experiment_model_identity_invalid",
                "The configured model endpoint cannot serve as an exact model identity",
                HTTPStatus.UNPROCESSABLE_ENTITY,
            )

    async def _compiled_scenario(self, contract: ExperimentContract) -> CompiledSupportScenario:
        prefix = "case."
        if not contract.scenario.scenario_id.startswith(prefix):
            _operator_error(
                "experiment_case_identity_invalid",
                "The persisted scenario is not a support regression case",
                HTTPStatus.CONFLICT,
            )
        try:
            case_id = UUID(hex=contract.scenario.scenario_id.removeprefix(prefix))
            case_version = int(contract.scenario.version)
        except ValueError:
            _operator_error(
                "experiment_case_identity_invalid",
                "The persisted support case reference is invalid",
                HTTPStatus.CONFLICT,
            )
        async with self._session_factory() as session:
            case = await RegressionCaseService(
                SqlAlchemyRegressionCaseRepository(session)
            ).get_case(case_id=case_id, case_version=case_version)
        try:
            scenario = SupportWorldPlugin(self._current_plugin_ref()).compile_case(case)
        except ValueError:
            _operator_error(
                "experiment_case_not_executable",
                "The persisted case cannot compile into the approved support world",
                HTTPStatus.CONFLICT,
            )
        if scenario.reference != contract.scenario:
            _operator_error(
                "experiment_case_identity_changed",
                "The persisted case no longer matches the experiment scenario identity",
                HTTPStatus.CONFLICT,
            )
        return scenario

    async def _compiled_suite_scenarios(
        self,
        contract: SupportSuiteExperimentContract,
        plugin: SupportWorldPlugin,
    ) -> tuple[CompiledSupportScenario, ...]:
        """Re-resolve every exact suite member before v2 execution admission."""
        async with self._session_factory() as session:
            resolved_suite, scenarios = await self._resolve_compiled_suite(
                session,
                suite_id=contract.suite.suite_id,
                suite_version=contract.suite.suite_version,
                plugin=plugin,
            )
        if resolved_suite != contract.suite:
            _operator_error(
                "experiment_suite_members_changed",
                "The saved suite membership or case identities no longer match the experiment",
                HTTPStatus.CONFLICT,
            )
        return scenarios

    async def _resolve_compiled_suite(
        self,
        session: AsyncSession,
        *,
        suite_id: UUID,
        suite_version: int,
        plugin: SupportWorldPlugin,
    ) -> tuple[SupportSuiteRef, tuple[CompiledSupportScenario, ...]]:
        """Load one exact saved suite, verify its stored membership hash, and compile its cases."""
        case_repository = SqlAlchemyRegressionCaseRepository(session)
        suite_repository = SqlAlchemySuiteRepository(session)
        try:
            suite = await SuiteService(suite_repository, case_repository).get_suite(
                suite_id=suite_id,
                suite_version=suite_version,
            )
        except ValueError:
            _operator_error(
                "experiment_suite_members_invalid",
                "The saved suite membership cannot be read as exact case versions",
                HTTPStatus.CONFLICT,
            )

        stored_suite = await suite_repository.suite_version(suite_id, suite_version)
        computed_members_hash = suite_members_hash(suite.members)
        member_ids = tuple(member.case_id for member in suite.members)
        if (
            stored_suite is None
            or stored_suite.members_hash != computed_members_hash
            or len(member_ids) < 3
            or len(set(member_ids)) != len(member_ids)
        ):
            _operator_error(
                "experiment_suite_members_invalid",
                "The saved suite must contain at least three distinct hash-verified cases",
                HTTPStatus.CONFLICT,
            )

        cases = RegressionCaseService(case_repository)
        compiled_scenarios: list[CompiledSupportScenario] = []
        members: list[SupportSuiteMemberRef] = []
        for member in suite.members:
            case = await cases.get_case(
                case_id=member.case_id,
                case_version=member.case_version,
            )
            try:
                scenario = plugin.compile_case(case)
            except ValueError:
                _operator_error(
                    "experiment_suite_member_not_executable",
                    "Every saved suite member must be an approved executable support case",
                    HTTPStatus.CONFLICT,
                )
            compiled_scenarios.append(scenario)
            members.append(
                SupportSuiteMemberRef(
                    case_id=member.case_id,
                    case_version=member.case_version,
                    scenario=scenario.reference,
                )
            )

        try:
            suite_ref = SupportSuiteRef(
                suite_id=suite.suite_id,
                suite_version=suite.suite_version,
                members_hash=stored_suite.members_hash,
                members=tuple(members),
            )
        except ValueError:
            _operator_error(
                "experiment_suite_members_invalid",
                "The saved suite members do not match their immutable support scenarios",
                HTTPStatus.CONFLICT,
            )
        return suite_ref, tuple(compiled_scenarios)

    def _runtime_bindings(
        self,
        contract: ExperimentContract | SupportSuiteExperimentContract,
        inputs: SupportExperimentStartInputs,
    ) -> SupportRuntimeBindings:
        artifact = self._current_runtime_artifact()
        if contract.baseline.artifact != artifact or contract.candidate.artifact != artifact:
            _operator_error(
                "experiment_runtime_identity_changed",
                "The persisted runtime identity no longer matches the built-in support runtime",
                HTTPStatus.CONFLICT,
            )
        baseline_model, baseline_runtime = self._baseline_model()
        candidate_model, candidate_runtime = self._candidate_model(
            contract.candidate_change,
            baseline_model=baseline_model,
            baseline_runtime=baseline_runtime,
        )
        self._require_model_endpoint_identity(contract.baseline.model, baseline_model)
        self._require_model_endpoint_identity(contract.candidate.model, candidate_model)
        baseline_prompt = self._pinned_prompt(contract.baseline.prompt, inputs.baseline_prompt)
        candidate_input = inputs.candidate_prompt or inputs.baseline_prompt
        candidate_prompt = self._pinned_prompt(contract.candidate.prompt, candidate_input)
        expected_evaluators = self._current_evaluators()
        if contract.evaluators != expected_evaluators:
            _operator_error(
                "experiment_evaluator_identity_changed",
                "The built-in evaluator identities no longer match the experiment",
                HTTPStatus.CONFLICT,
            )
        return SupportRuntimeBindings(
            artifact=artifact,
            models=_deduplicate_models(
                (
                    PinnedModelRuntime(reference=baseline_model, model_config=baseline_runtime),
                    PinnedModelRuntime(reference=candidate_model, model_config=candidate_runtime),
                )
            ),
            prompts=_deduplicate_prompts((baseline_prompt, candidate_prompt)),
        )

    @staticmethod
    def _require_model_endpoint_identity(stored: ModelRef, current: ModelRef) -> None:
        """Require a stored model alias to retain its configured endpoint binding."""
        if stored.provider != current.provider or stored.model_id != current.model_id:
            _operator_error(
                "experiment_model_identity_changed",
                "The configured provider or model alias no longer matches the experiment",
                HTTPStatus.CONFLICT,
            )
        if not _is_endpoint_fingerprint(stored.version):
            _operator_error(
                "experiment_model_endpoint_unpinned",
                "This v1 experiment lacks an endpoint binding and must be recreated",
                HTTPStatus.CONFLICT,
            )
        if stored.version != current.version:
            _operator_error(
                "experiment_model_endpoint_changed",
                "The configured model endpoint no longer matches the experiment",
                HTTPStatus.CONFLICT,
            )

    @staticmethod
    def _pinned_prompt(expected: PromptRef, runtime: SupportPromptRuntime) -> PinnedPromptRuntime:
        actual = runtime.as_reference().as_prompt_ref()
        if actual != expected:
            _operator_error(
                "experiment_prompt_identity_mismatch",
                "The supplied prompt body does not match the persisted prompt identity",
                HTTPStatus.UNPROCESSABLE_ENTITY,
            )
        return PinnedPromptRuntime(
            reference=actual,
            answer_instructions=runtime.content.get_secret_value(),
        )

    @staticmethod
    def _stored_contract(
        record: ExperimentRecord,
    ) -> ExperimentContract | SupportSuiteExperimentContract:
        try:
            contract = parse_experiment_contract(record.contract)
        except ValueError:
            _operator_error(
                "experiment_contract_invalid",
                "The persisted experiment contract is invalid",
                HTTPStatus.CONFLICT,
            )
        if contract.experiment_id != record.id or contract.content_hash != record.contract_hash:
            _operator_error(
                "experiment_contract_invalid",
                "The persisted experiment contract does not match its record",
                HTTPStatus.CONFLICT,
            )
        return contract


async def _database_identity_for_url(database_url: str) -> _DatabaseIdentity:
    """Read a control-plane database identity from the server, not its URL spelling."""
    engine = create_async_engine(database_url, poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            return await _observed_database_identity(connection)
    finally:
        await engine.dispose()


async def _observed_database_identity(connection: AsyncConnection) -> _DatabaseIdentity:
    """Return the PostgreSQL cluster and database identity visible to this connection."""
    result = await connection.execute(_DATABASE_IDENTITY_QUERY)
    row = result.mappings().one()
    system_identifier = row["system_identifier"]
    database_name = row["database_name"]
    if (
        not isinstance(system_identifier, str)
        or not system_identifier
        or not isinstance(database_name, str)
        or not database_name
    ):
        raise ValueError("PostgreSQL did not return a complete database identity")
    return _DatabaseIdentity(
        system_identifier=system_identifier,
        database_name=database_name,
    )


def _normalized_model_endpoint(runtime: ModelConfig) -> str:
    """Normalize endpoint routing data while excluding credentials and query secrets."""
    configured = runtime.base_url or _DEFAULT_MODEL_ENDPOINTS[runtime.provider]
    parsed = urlsplit(configured)
    if not parsed.scheme or parsed.hostname is None:
        raise ValueError("model endpoint must include a scheme and hostname")
    if (
        parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError(
            "model endpoint must not embed credentials, query parameters, or fragments"
        )
    try:
        port = parsed.port
    except ValueError:
        raise ValueError("model endpoint port is invalid") from None

    scheme = parsed.scheme.casefold()
    host = parsed.hostname.casefold()
    rendered_host = f"[{host}]" if ":" in host else host
    default_port = {"http": 80, "https": 443}.get(scheme)
    rendered_port = "" if port is None or port == default_port else f":{port}"
    path = parsed.path.rstrip("/") or "/"
    return f"{scheme}://{rendered_host}{rendered_port}{path}"


def _is_endpoint_fingerprint(version: str) -> bool:
    """Recognize the endpoint fingerprint format added after v1 contracts shipped."""
    fingerprint = version.removeprefix(_ENDPOINT_FINGERPRINT_PREFIX)
    return (
        version.startswith(_ENDPOINT_FINGERPRINT_PREFIX)
        and len(fingerprint) == 64
        and all(character in "0123456789abcdef" for character in fingerprint)
    )


def _deduplicate_models(
    bindings: tuple[PinnedModelRuntime, ...],
) -> tuple[PinnedModelRuntime, ...]:
    unique: list[PinnedModelRuntime] = []
    for binding in bindings:
        if binding.reference not in {item.reference for item in unique}:
            unique.append(binding)
    return tuple(unique)


def _deduplicate_prompts(
    bindings: tuple[PinnedPromptRuntime, ...],
) -> tuple[PinnedPromptRuntime, ...]:
    unique: list[PinnedPromptRuntime] = []
    for binding in bindings:
        if binding.reference not in {item.reference for item in unique}:
            unique.append(binding)
    return tuple(unique)


def _operator_error(code: str, message: str, status_code: HTTPStatus) -> NoReturn:
    raise ExperimentOperatorError(code=code, message=message, status_code=int(status_code))
