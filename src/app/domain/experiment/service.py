"""Domain service for durable support-experiment lifecycle and publication."""

import json
from collections.abc import Callable
from datetime import UTC, datetime
from hashlib import sha256
from typing import Any
from uuid import UUID

from app.domain.experiment.contracts import (
    ExperimentContract,
    IterationIdentity,
    SupportSuiteExperimentContract,
    build_iteration_plan,
    parse_experiment_contract,
)
from app.domain.experiment.engine import CompletedIteration, FailedIteration
from app.domain.experiment.events import ExperimentEvent, ExperimentEventKind
from app.domain.experiment.models import (
    ExperimentEventRecord,
    ExperimentIterationOutcomeRecord,
    ExperimentRecord,
    ExperimentResultRecord,
)
from app.domain.experiment.repository import ExperimentRepository
from app.domain.experiment.state_machine import (
    ExperimentConflictError,
    ExperimentNotFoundError,
    ExperimentPersistenceError,
    ExperimentStatus,
    IterationOutcomeStatus,
    require_safe_error_code,
    require_transition,
)
from app.domain.experiment.support_results import (
    SupportIterationResult,
    SupportMutationAttribution,
    SupportResult,
    compute_support_result_hash,
    parse_support_experiment_result,
    validate_support_suite_iteration_evidence,
)
from app.domain.simulation.runner import SimulationRun

Clock = Callable[[], datetime]


class ExperimentService:
    """Own the persisted experiment state machine and immutable result publication."""

    def __init__(self, repository: ExperimentRepository, *, clock: Clock | None = None) -> None:
        self._repository = repository
        self._clock = clock or (lambda: datetime.now(UTC))

    async def create(
        self,
        contract: ExperimentContract | SupportSuiteExperimentContract,
    ) -> ExperimentRecord:
        """Persist the exact validated contract before any execution can start."""
        return await self._repository.create(
            experiment_id=contract.experiment_id,
            contract=contract.model_dump(mode="json"),
            contract_hash=contract.content_hash,
            status=ExperimentStatus.PENDING,
        )

    async def start_execution(
        self,
        experiment_id: UUID,
        execution_id: UUID,
    ) -> ExperimentRecord:
        """Bind one execution attempt and move a pending experiment to running."""
        record = await self._require_record(experiment_id)
        self._require_transition(record, ExperimentStatus.RUNNING)
        updated = await self._repository.transition(
            experiment_id,
            expected_status=ExperimentStatus.PENDING,
            expected_execution_id=None,
            status=ExperimentStatus.RUNNING,
            execution_id=execution_id,
            error_code=None,
            started_at=self._clock(),
            finished_at=None,
        )
        if updated is None:
            raise ExperimentConflictError(
                "experiment lifecycle changed before execution could start"
            )
        return updated

    async def fail(
        self,
        experiment_id: UUID,
        execution_id: UUID | None,
        *,
        error_code: str,
    ) -> ExperimentRecord:
        """Record a safe operational failure and terminally close the experiment."""
        require_safe_error_code(error_code)
        return await self._finish(
            experiment_id,
            execution_id,
            status=ExperimentStatus.FAILED,
            error_code=error_code,
        )

    async def cancel(
        self,
        experiment_id: UUID,
        execution_id: UUID | None,
    ) -> ExperimentRecord:
        """Cancel an experiment through the same terminal state-machine boundary."""
        return await self._finish(
            experiment_id,
            execution_id,
            status=ExperimentStatus.CANCELLED,
            error_code=None,
        )

    async def record_events(
        self,
        experiment_id: UUID,
        execution_id: UUID,
        events: list[ExperimentEvent],
    ) -> int:
        """Flush a validated event batch before callers make it available for replay."""
        record = await self._require_running_attempt(experiment_id, execution_id)
        contract = self._stored_contract(record)
        by_sequence: dict[int, ExperimentEvent] = {}
        for event in events:
            if event.experiment_id != experiment_id or event.execution_id != execution_id:
                raise ExperimentPersistenceError(
                    "event identity does not match the active experiment execution"
                )
            if event.iteration is not None:
                self._expected_iteration(contract, event.iteration)
            previous = by_sequence.setdefault(event.sequence, event)
            if self._canonical_json(event.model_dump(mode="json")) != self._canonical_json(
                previous.model_dump(mode="json")
            ):
                raise ExperimentConflictError(
                    "event sequence is replayed with different canonical content"
                )
        return await self._repository.record_events(list(by_sequence.values()))

    async def replay_events(
        self,
        experiment_id: UUID,
        execution_id: UUID,
        *,
        after_sequence: int = 0,
        limit: int = 100,
    ) -> list[ExperimentEventRecord]:
        """Return persisted events after a cursor in their original sequence order."""
        if after_sequence < 0:
            raise ExperimentPersistenceError("event cursor cannot be negative")
        if not 1 <= limit <= 1_000:
            raise ExperimentPersistenceError("event replay limit must be between 1 and 1000")
        await self._require_attempt(experiment_id, execution_id)
        return await self._repository.get_events(
            experiment_id,
            execution_id,
            after_sequence=after_sequence,
            limit=limit,
        )

    async def record_iteration_outcome(
        self,
        experiment_id: UUID,
        execution_id: UUID,
        outcome: CompletedIteration[SimulationRun] | FailedIteration,
    ) -> ExperimentIterationOutcomeRecord:
        """Persist one planned terminal outcome and its immutable completed evidence."""
        record = await self._require_running_attempt(experiment_id, execution_id)
        contract = self._stored_contract(record)
        expected_identity = self._expected_iteration(contract, outcome.identity)
        if isinstance(outcome, FailedIteration):
            require_safe_error_code(outcome.error_code)
            status = IterationOutcomeStatus.FAILED
            error_code: str | None = outcome.error_code
            evidence: dict[str, Any] | None = None
            evidence_hash: str | None = None
        else:
            if not isinstance(outcome.result, SimulationRun):
                raise ExperimentPersistenceError(
                    "completed iteration evidence must be a SimulationRun"
                )
            status = IterationOutcomeStatus.COMPLETED
            error_code = None
            evidence_model = SupportIterationResult(
                identity=expected_identity,
                run=outcome.result,
                attributed_mutations=tuple(
                    SupportMutationAttribution(mutation=mutation)
                    for mutation in outcome.result.mutations
                ),
            )
            if isinstance(contract, SupportSuiteExperimentContract):
                validate_support_suite_iteration_evidence(
                    contract,
                    expected_identity,
                    evidence_model,
                )
            evidence = evidence_model.model_dump(mode="json")
            evidence_hash = self._content_hash(evidence)

        persisted = await self._repository.record_iteration_outcome(
            experiment_id,
            execution_id,
            expected_identity,
            status=status,
            error_code=error_code,
            evidence=evidence,
            evidence_hash=evidence_hash,
        )
        if not self._matches_outcome(
            persisted,
            expected_identity,
            status=status,
            error_code=error_code,
            evidence=evidence,
            evidence_hash=evidence_hash,
        ):
            raise ExperimentConflictError(
                "iteration outcome conflicts with immutable persisted evidence"
            )
        return persisted

    async def publish_result(
        self,
        experiment_id: UUID,
        execution_id: UUID,
        result: SupportResult,
        *,
        completion_event: ExperimentEvent,
    ) -> ExperimentResultRecord:
        """Atomically store a result, completion event, and completed lifecycle state."""
        record = await self._require_running_attempt(experiment_id, execution_id)
        contract = self._stored_contract(record)
        expected_plan = build_iteration_plan(contract)
        if result.content_hash != compute_support_result_hash(result):
            raise ExperimentPersistenceError(
                "result content hash does not match its complete content"
            )
        result = self._validated_result(result)

        if result.execution_id != execution_id:
            raise ExperimentPersistenceError("result execution ID differs from the active attempt")
        if result.contract != contract:
            raise ExperimentPersistenceError("result contract differs from the persisted contract")
        if result.plan != expected_plan:
            raise ExperimentPersistenceError(
                "result iteration plan differs from the contract schedule"
            )
        if result.content_hash != compute_support_result_hash(result):
            raise ExperimentPersistenceError(
                "result content hash does not match its complete content"
            )

        await self._verify_result_evidence(
            experiment_id,
            execution_id,
            contract,
            result,
        )

        expected_completion_sequence = (2 * len(expected_plan.iterations)) + 2
        if (
            completion_event.experiment_id != experiment_id
            or completion_event.execution_id != execution_id
            or completion_event.kind != ExperimentEventKind.EXPERIMENT_COMPLETED
            or completion_event.iteration is not None
            or completion_event.sequence != expected_completion_sequence
        ):
            raise ExperimentPersistenceError(
                "completion event does not match the completed experiment execution"
            )
        await self.record_events(experiment_id, execution_id, [completion_event])

        persisted = await self._repository.publish_result(
            experiment_id,
            execution_id,
            contract_hash=record.contract_hash,
            content_hash=result.content_hash,
            result=result.model_dump(mode="json"),
            completed_at=self._clock(),
        )
        if persisted is None:
            raise ExperimentConflictError(
                "result publication conflicts with the experiment lifecycle"
            )
        return persisted

    async def get_result(self, experiment_id: UUID) -> SupportResult | None:
        """Load only a result whose stored JSON, hashes, contract, and lifecycle agree."""
        record = await self._require_record(experiment_id)
        persisted = await self._repository.get_result_record(experiment_id)
        if persisted is None:
            return None

        contract = self._stored_contract(record)
        if self._status(record) is not ExperimentStatus.COMPLETED:
            raise ExperimentPersistenceError(
                "persisted result exists for an experiment that is not completed"
            )
        if (
            record.execution_id is None
            or persisted.execution_id != record.execution_id
            or persisted.contract_hash != record.contract_hash
        ):
            raise ExperimentPersistenceError(
                "persisted result metadata differs from the experiment record"
            )
        try:
            result = parse_support_experiment_result(persisted.result)
        except ValueError as error:
            raise ExperimentPersistenceError("persisted result JSON is invalid") from error
        if self._canonical_json(persisted.result) != self._canonical_json(
            result.model_dump(mode="json")
        ):
            raise ExperimentPersistenceError(
                "persisted result JSON is not its canonical representation"
            )
        if (
            result.content_hash != compute_support_result_hash(result)
            or persisted.content_hash != result.content_hash
        ):
            raise ExperimentPersistenceError("persisted result content hash is invalid")
        if result.execution_id != persisted.execution_id or result.contract != contract:
            raise ExperimentPersistenceError(
                "persisted result execution or contract differs from its experiment"
            )
        if result.plan != build_iteration_plan(contract):
            raise ExperimentPersistenceError(
                "persisted result iteration plan differs from the contract schedule"
            )
        await self._verify_result_evidence(
            experiment_id,
            persisted.execution_id,
            contract,
            result,
        )
        return result

    async def _finish(
        self,
        experiment_id: UUID,
        execution_id: UUID | None,
        *,
        status: ExperimentStatus,
        error_code: str | None,
    ) -> ExperimentRecord:
        record = await self._require_record(experiment_id)
        self._require_transition(record, status)
        if record.execution_id != execution_id:
            raise ExperimentPersistenceError(
                "execution ID does not match the active experiment attempt"
            )
        updated = await self._repository.transition(
            experiment_id,
            expected_status=self._status(record),
            expected_execution_id=execution_id,
            status=status,
            execution_id=execution_id,
            error_code=error_code,
            started_at=None,
            finished_at=self._clock(),
        )
        if updated is None:
            raise ExperimentConflictError(
                "experiment lifecycle changed before it could be finalized"
            )
        return updated

    async def _require_record(self, experiment_id: UUID) -> ExperimentRecord:
        record = await self._repository.get_by_id(experiment_id)
        if record is None:
            raise ExperimentNotFoundError(f"experiment {experiment_id} was not found")
        return record

    async def _require_attempt(
        self,
        experiment_id: UUID,
        execution_id: UUID,
    ) -> ExperimentRecord:
        record = await self._require_record(experiment_id)
        if record.execution_id != execution_id:
            raise ExperimentPersistenceError("execution ID does not match the persisted experiment")
        return record

    async def _require_running_attempt(
        self,
        experiment_id: UUID,
        execution_id: UUID,
    ) -> ExperimentRecord:
        record = await self._require_attempt(experiment_id, execution_id)
        if self._status(record) is not ExperimentStatus.RUNNING:
            raise ExperimentPersistenceError(
                "experiment is not running and cannot accept execution data"
            )
        return record

    def _require_transition(self, record: ExperimentRecord, target: ExperimentStatus) -> None:
        require_transition(self._status(record), target)

    @staticmethod
    def _status(record: ExperimentRecord) -> ExperimentStatus:
        try:
            return ExperimentStatus(record.status)
        except ValueError as error:
            raise ExperimentPersistenceError(
                "persisted experiment has an invalid lifecycle"
            ) from error

    @staticmethod
    def _stored_contract(
        record: ExperimentRecord,
    ) -> ExperimentContract | SupportSuiteExperimentContract:
        try:
            contract = parse_experiment_contract(record.contract)
        except ValueError as error:
            raise ExperimentPersistenceError("persisted experiment contract is invalid") from error
        if ExperimentService._canonical_json(record.contract) != ExperimentService._canonical_json(
            contract.model_dump(mode="json")
        ):
            raise ExperimentPersistenceError(
                "persisted experiment contract is not its canonical representation"
            )
        if contract.experiment_id != record.id or contract.content_hash != record.contract_hash:
            raise ExperimentPersistenceError(
                "persisted experiment contract hash does not match its record"
            )
        return contract

    @staticmethod
    def _expected_iteration(
        contract: ExperimentContract | SupportSuiteExperimentContract,
        identity: IterationIdentity,
    ) -> IterationIdentity:
        if identity.experiment_id != contract.experiment_id:
            raise ExperimentPersistenceError("iteration belongs to a different experiment")
        for expected in build_iteration_plan(contract).iterations:
            if identity == expected:
                return expected
        raise ExperimentPersistenceError("iteration identity is not in the contract schedule")

    @staticmethod
    def _matches_identity(
        outcome: ExperimentIterationOutcomeRecord,
        identity: IterationIdentity,
    ) -> bool:
        return (
            outcome.iteration_id == identity.iteration_id
            and outcome.ordinal == identity.ordinal
            and outcome.variant == identity.variant.value
            and outcome.repetition == identity.repetition
        )

    @classmethod
    def _matches_outcome(
        cls,
        outcome: ExperimentIterationOutcomeRecord,
        identity: IterationIdentity,
        *,
        status: IterationOutcomeStatus,
        error_code: str | None,
        evidence: dict[str, Any] | None,
        evidence_hash: str | None,
    ) -> bool:
        if (
            outcome.status != status.value
            or outcome.error_code != error_code
            or outcome.evidence_hash != evidence_hash
            or not cls._matches_identity(outcome, identity)
        ):
            return False
        if outcome.evidence is None or evidence is None:
            return outcome.evidence is None and evidence is None
        return cls._canonical_json(outcome.evidence) == cls._canonical_json(evidence)

    @classmethod
    def _stored_iteration_evidence(
        cls,
        outcome: ExperimentIterationOutcomeRecord,
        identity: IterationIdentity,
        contract: ExperimentContract | SupportSuiteExperimentContract,
    ) -> SupportIterationResult:
        if outcome.evidence is None or outcome.evidence_hash is None:
            raise ExperimentPersistenceError(
                "completed iteration is missing immutable persisted evidence"
            )
        if cls._content_hash(outcome.evidence) != outcome.evidence_hash:
            raise ExperimentPersistenceError(
                "completed iteration evidence hash does not match its content"
            )
        try:
            evidence = SupportIterationResult.model_validate(outcome.evidence)
        except ValueError as error:
            raise ExperimentPersistenceError(
                "completed iteration evidence is not valid support-run data"
            ) from error
        if cls._canonical_json(outcome.evidence) != cls._canonical_json(
            evidence.model_dump(mode="json")
        ):
            raise ExperimentPersistenceError(
                "completed iteration evidence is not in canonical form"
            )
        if evidence.identity != identity:
            raise ExperimentPersistenceError(
                "completed iteration evidence differs from the contract schedule"
            )
        if isinstance(contract, SupportSuiteExperimentContract):
            try:
                validate_support_suite_iteration_evidence(contract, identity, evidence)
            except ValueError as error:
                raise ExperimentPersistenceError(
                    "completed iteration evidence differs from the support-suite contract"
                ) from error
        return evidence

    async def _verify_result_evidence(
        self,
        experiment_id: UUID,
        execution_id: UUID,
        contract: ExperimentContract | SupportSuiteExperimentContract,
        result: SupportResult,
    ) -> None:
        """Require every stored terminal outcome to exactly match published evidence."""
        expected_identities = build_iteration_plan(contract).iterations
        result_identities = tuple(iteration.identity for iteration in result.iterations)
        if result_identities != expected_identities:
            raise ExperimentPersistenceError(
                "result iterations are missing, duplicated, or differ from the planned identities"
            )

        outcomes = await self._repository.get_iteration_outcomes(experiment_id, execution_id)
        if len(outcomes) != len(expected_identities):
            raise ExperimentPersistenceError(
                "result cannot publish while planned iterations are missing"
            )
        for outcome, identity, proposed in zip(
            outcomes,
            expected_identities,
            result.iterations,
            strict=True,
        ):
            if not self._matches_identity(outcome, identity):
                raise ExperimentPersistenceError(
                    "persisted iteration outcomes differ from the contract schedule"
                )
            if outcome.status != IterationOutcomeStatus.COMPLETED.value:
                raise ExperimentPersistenceError(
                    "result cannot publish because at least one iteration failed"
                )
            evidence = self._stored_iteration_evidence(outcome, identity, contract)
            if self._canonical_json(evidence.model_dump(mode="json")) != self._canonical_json(
                proposed.model_dump(mode="json")
            ):
                raise ExperimentConflictError(
                    "proposed result evidence differs from immutable persisted evidence"
                )

    @classmethod
    def _validated_result(cls, result: SupportResult) -> SupportResult:
        """Reparse a proposed result so model-copy mutations cannot bypass its invariants."""
        payload = result.model_dump(mode="json")
        try:
            validated = parse_support_experiment_result(payload)
        except ValueError as error:
            raise ExperimentPersistenceError("result JSON is invalid") from error
        if cls._canonical_json(payload) != cls._canonical_json(validated.model_dump(mode="json")):
            raise ExperimentPersistenceError("result JSON is not its canonical representation")
        return validated

    @staticmethod
    def _canonical_json(value: dict[str, Any]) -> str:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)

    @classmethod
    def _content_hash(cls, value: dict[str, Any]) -> str:
        return sha256(cls._canonical_json(value).encode("utf-8")).hexdigest()
