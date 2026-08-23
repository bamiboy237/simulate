"""Domain service coordinating investigation lifecycle, persistence, and state transitions."""

import hashlib
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID, uuid4

from app.domain.investigation.errors import (
    InvalidBridgeTokenError,
    InvalidStateTransitionError,
    InvestigationNotFoundError,
    TerminalStateImmutableError,
)
from app.domain.investigation.models import (
    InvestigationEventRecord,
    InvestigationMessageRecord,
    InvestigationRecord,
)
from app.domain.investigation.repository import InvestigationRepository
from app.domain.investigation.schemas import (
    TERMINAL_STATUSES,
    InvestigationCreateRequest,
    InvestigationDetailResponse,
    InvestigationResponse,
    InvestigationStatus,
    InvestigationSummaryResponse,
)
from app.domain.runner.schemas import EventType, RunnerEvent

# Valid state machine transitions
ALLOWED_TRANSITIONS: dict[InvestigationStatus, set[InvestigationStatus]] = {
    InvestigationStatus.PENDING: {
        InvestigationStatus.PROVISIONING,
        InvestigationStatus.CANCELLED,
        InvestigationStatus.FAILED,
    },
    InvestigationStatus.PROVISIONING: {
        InvestigationStatus.RUNNING,
        InvestigationStatus.FAILED,
        InvestigationStatus.CANCELLED,
    },
    InvestigationStatus.RUNNING: {
        InvestigationStatus.COMPLETED,
        InvestigationStatus.FAILED,
        InvestigationStatus.CANCELLED,
    },
    InvestigationStatus.COMPLETED: set(),
    InvestigationStatus.FAILED: set(),
    InvestigationStatus.CANCELLED: set(),
}


def hash_token(token: str) -> str:
    """Compute SHA-256 hash of a bearer token."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class InvestigationService:
    """Owns investigation state machine, event intake, chat, and stale sweep."""

    def __init__(self, repository: InvestigationRepository) -> None:
        self._repository = repository

    async def start(
        self,
        request: InvestigationCreateRequest,
        *,
        investigation_id: UUID | None = None,
    ) -> tuple[InvestigationResponse, str]:
        """Create a new investigation in pending status and generate a bridge token."""
        inv_id = investigation_id or uuid4()
        token = secrets.token_urlsafe(32)
        token_hash = hash_token(token)

        world_name = request.world_ref.get("world_id", "unknown")
        slice_name = request.slice_ref.slice_name
        task_brief = request.task_brief or (
            f"# Investigation Task Brief\n"
            f"World: {world_name} | Slice: {slice_name}\n"
            f"Please investigate the observed support failure and submit findings."
        )

        tested_ref_dump = (
            request.tested_agent_ref.model_dump()
            if request.tested_agent_ref
            else None
        )

        record = await self._repository.create(
            investigation_id=inv_id,
            status=InvestigationStatus.PENDING.value,
            trace_ref=request.trace_ref,
            world_ref=request.world_ref,
            slice_ref=request.slice_ref.model_dump(),
            investigator_ref=request.investigator_ref.model_dump(),
            tested_agent_ref=tested_ref_dump,
            task_brief=task_brief,
            bridge_token_hash=token_hash,
        )

        resp = InvestigationResponse(
            id=record.id,
            status=InvestigationStatus(record.status),
            task_brief=record.task_brief,
            bridge_token=token,
            created_at=record.created_at,
            started_at=record.started_at,
            finished_at=record.finished_at,
            error=record.error,
        )
        return resp, token

    async def transition_status(
        self,
        investigation_id: UUID,
        target_status: InvestigationStatus,
        *,
        error: str | None = None,
    ) -> InvestigationRecord:
        """Execute a state machine transition, rejecting invalid or terminal modifications."""
        record = await self._repository.get_by_id(investigation_id)
        if record is None:
            raise InvestigationNotFoundError(investigation_id)

        current_status = InvestigationStatus(record.status)
        if current_status in TERMINAL_STATUSES:
            raise TerminalStateImmutableError(current_status.value)

        if target_status not in ALLOWED_TRANSITIONS.get(current_status, set()):
            raise InvalidStateTransitionError(current_status.value, target_status.value)

        now = datetime.now(timezone.utc)
        started_at = now if target_status == InvestigationStatus.RUNNING else None
        finished_at = now if target_status in TERMINAL_STATUSES else None

        updated = await self._repository.update_status(
            investigation_id,
            target_status.value,
            error=error,
            started_at=started_at,
            finished_at=finished_at,
        )
        if updated is None:
            raise InvestigationNotFoundError(investigation_id)
        return updated

    async def get_by_id(self, investigation_id: UUID) -> InvestigationDetailResponse:
        """Retrieve full investigation details."""
        record = await self._repository.get_by_id(investigation_id)
        if record is None:
            raise InvestigationNotFoundError(investigation_id)

        summary_resp: InvestigationSummaryResponse | None = None
        summary_record = await self._repository.get_summary(investigation_id)
        if summary_record is not None:
            summary_resp = InvestigationSummaryResponse(
                id=summary_record.id,
                investigation_id=summary_record.investigation_id,
                findings=summary_record.findings,
                next_step=summary_record.next_step,
                evidence_refs=summary_record.evidence_refs,
                created_at=summary_record.created_at,
            )

        return InvestigationDetailResponse(
            id=record.id,
            status=InvestigationStatus(record.status),
            task_brief=record.task_brief,
            trace_ref=record.trace_ref,
            world_ref=record.world_ref,
            slice_ref=record.slice_ref,
            investigator_ref=record.investigator_ref,
            tested_agent_ref=record.tested_agent_ref,
            error=record.error,
            last_heartbeat_at=record.last_heartbeat_at,
            created_at=record.created_at,
            started_at=record.started_at,
            finished_at=record.finished_at,
            summary=summary_resp,
        )

    async def list_investigations(
        self,
        *,
        limit: int = 50,
        offset: int = 0,
    ) -> list[InvestigationResponse]:
        """List investigation summaries."""
        records = await self._repository.list_investigations(limit=limit, offset=offset)
        return [
            InvestigationResponse(
                id=rec.id,
                status=InvestigationStatus(rec.status),
                task_brief=rec.task_brief,
                created_at=rec.created_at,
                started_at=rec.started_at,
                finished_at=rec.finished_at,
                error=rec.error,
            )
            for rec in records
        ]

    async def verify_bridge_token(self, token: str) -> InvestigationRecord:
        """Verify bearer token from bridge and return associated investigation."""
        token_hash = hash_token(token)
        record = await self._repository.get_by_bridge_token_hash(token_hash)
        if record is None:
            raise InvalidBridgeTokenError()
        return record

    async def record_events(
        self,
        token: str,
        events: list[RunnerEvent],
    ) -> int:
        """Persist a batch of events idempotently and update heartbeat/summary."""
        record = await self.verify_bridge_token(token)
        current_status = InvestigationStatus(record.status)
        if current_status in TERMINAL_STATUSES:
            # Events received after completion/cancellation are harmlessly dropped
            return 0

        # Idempotent insert into PostgreSQL
        inserted = await self._repository.record_events(record.id, events)

        # Inspect events for stateful transitions
        now = datetime.now(timezone.utc)
        for event in events:
            if event.type == EventType.heartbeat:
                await self._repository.update_heartbeat(record.id, now)
            elif event.type == EventType.summary_submitted:
                findings = str(event.payload.get("findings", "Investigation completed"))
                next_step = str(event.payload.get("next_step", "None proposed"))
                evidence_refs = event.payload.get("evidence_refs", [])
                if isinstance(evidence_refs, list):
                    refs_list: list[dict[str, Any]] = [
                        r for r in evidence_refs if isinstance(r, dict)
                    ]
                else:
                    refs_list = []
                await self._repository.save_summary(
                    record.id,
                    findings=findings,
                    next_step=next_step,
                    evidence_refs=refs_list,
                )
                await self.transition_status(record.id, InvestigationStatus.COMPLETED)
            elif event.type == EventType.error and current_status == InvestigationStatus.RUNNING:
                error_msg = str(event.payload.get("error", "Harness encountered fatal error"))
                await self.transition_status(
                    record.id,
                    InvestigationStatus.FAILED,
                    error=error_msg,
                )

        return inserted

    async def get_events_stream(
        self,
        investigation_id: UUID,
        *,
        after_seq: int = 0,
        limit: int = 100,
    ) -> list[InvestigationEventRecord]:
        """Fetch ordered events from persistence for streaming or replaying."""
        record = await self._repository.get_by_id(investigation_id)
        if record is None:
            raise InvestigationNotFoundError(investigation_id)
        return await self._repository.get_events(
            investigation_id,
            after_seq=after_seq,
            limit=limit,
        )

    async def record_user_message(
        self,
        investigation_id: UUID,
        body: str,
        mode: str = "prompt",
    ) -> InvestigationMessageRecord:
        """Store a chat message to be picked up by the bridge long-poll."""
        record = await self._repository.get_by_id(investigation_id)
        if record is None:
            raise InvestigationNotFoundError(investigation_id)
        if InvestigationStatus(record.status) in TERMINAL_STATUSES:
            raise TerminalStateImmutableError(record.status)

        return await self._repository.record_message(
            investigation_id,
            sender="user",
            body=body,
            mode=mode,
        )

    async def poll_inbox(
        self,
        token: str,
        *,
        cursor: int = 0,
        limit: int = 50,
    ) -> list[InvestigationMessageRecord]:
        """Return new chat messages for the bridge."""
        record = await self.verify_bridge_token(token)
        return await self._repository.get_messages(
            record.id,
            after_id=cursor,
            limit=limit,
        )

    async def sweep_stale(self, silence_seconds: int = 90) -> list[UUID]:
        """Mark running investigations silent for >silence_seconds as failed (operational)."""
        now = datetime.now(timezone.utc)
        cutoff = now - timedelta(seconds=silence_seconds)
        stale_records = await self._repository.find_stale_running(cutoff)
        reconciled_ids: list[UUID] = []

        for record in stale_records:
            try:
                await self.transition_status(
                    record.id,
                    InvestigationStatus.FAILED,
                    error="operational: heartbeat silence timeout",
                )
                reconciled_ids.append(record.id)
            except Exception:
                # If race occurred, skip
                pass

        return reconciled_ids
