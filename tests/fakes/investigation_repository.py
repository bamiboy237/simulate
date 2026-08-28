"""In-memory fake investigation repository for fast, offline testing."""

from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from app.domain.investigation.models import (
    InvestigationEventRecord,
    InvestigationMessageRecord,
    InvestigationRecord,
    InvestigationSummaryRecord,
)
from app.domain.runner.schemas import RunnerEvent


class InMemoryInvestigationRepository:
    """In-memory test double implementing InvestigationRepository protocol."""

    def __init__(self) -> None:
        self.investigations: dict[UUID, InvestigationRecord] = {}
        self.events: dict[UUID, list[InvestigationEventRecord]] = {}
        self.messages: dict[UUID, list[InvestigationMessageRecord]] = {}
        self.summaries: dict[UUID, InvestigationSummaryRecord] = {}
        self._msg_id_counter = 0
        self._event_id_counter = 0

    async def create(
        self,
        *,
        investigation_id: UUID,
        status: str,
        trace_ref: dict[str, Any],
        world_ref: dict[str, Any],
        slice_ref: dict[str, Any],
        investigator_ref: dict[str, Any],
        tested_agent_ref: dict[str, Any] | None,
        task_brief: str,
        bridge_token_hash: str,
    ) -> InvestigationRecord:
        record = InvestigationRecord(
            id=investigation_id,
            status=status,
            trace_ref=trace_ref,
            world_ref=world_ref,
            slice_ref=slice_ref,
            investigator_ref=investigator_ref,
            tested_agent_ref=tested_agent_ref,
            task_brief=task_brief,
            bridge_token_hash=bridge_token_hash,
            created_at=datetime.now(timezone.utc),
        )
        self.investigations[investigation_id] = record
        self.events[investigation_id] = []
        self.messages[investigation_id] = []
        return record

    async def get_by_id(self, investigation_id: UUID) -> InvestigationRecord | None:
        return self.investigations.get(investigation_id)

    async def get_by_bridge_token_hash(self, token_hash: str) -> InvestigationRecord | None:
        for rec in self.investigations.values():
            if rec.bridge_token_hash == token_hash:
                return rec
        return None

    async def list_investigations(
        self,
        *,
        limit: int = 50,
        offset: int = 0,
    ) -> list[InvestigationRecord]:
        all_recs = sorted(
            self.investigations.values(),
            key=lambda r: r.created_at,
            reverse=True,
        )
        return all_recs[offset : offset + limit]

    async def update_status(
        self,
        investigation_id: UUID,
        status: str,
        *,
        error: str | None = None,
        started_at: datetime | None = None,
        finished_at: datetime | None = None,
    ) -> InvestigationRecord | None:
        record = self.investigations.get(investigation_id)
        if record is None:
            return None
        record.status = status
        if error is not None:
            record.error = error
        if started_at is not None:
            record.started_at = started_at
        if finished_at is not None:
            record.finished_at = finished_at
        return record

    async def update_heartbeat(
        self,
        investigation_id: UUID,
        timestamp: datetime,
    ) -> None:
        record = self.investigations.get(investigation_id)
        if record:
            record.last_heartbeat_at = timestamp

    async def record_events(
        self,
        investigation_id: UUID,
        events: list[RunnerEvent],
    ) -> int:
        if investigation_id not in self.events:
            self.events[investigation_id] = []

        existing_seqs = {e.seq for e in self.events[investigation_id]}
        inserted = 0

        for event in events:
            if event.seq not in existing_seqs:
                self._event_id_counter += 1
                type_str = event.type.value if hasattr(event.type, "value") else str(event.type)
                rec = InvestigationEventRecord(
                    id=self._event_id_counter,
                    investigation_id=investigation_id,
                    seq=event.seq,
                    type=type_str,
                    payload=event.payload,
                    emitted_at=event.emitted_at,
                )
                self.events[investigation_id].append(rec)
                existing_seqs.add(event.seq)
                inserted += 1

        return inserted

    async def get_events(
        self,
        investigation_id: UUID,
        *,
        after_seq: int = 0,
        limit: int = 100,
    ) -> list[InvestigationEventRecord]:
        event_list = self.events.get(investigation_id, [])
        filtered = [e for e in event_list if e.seq > after_seq]
        filtered.sort(key=lambda e: e.seq)
        return filtered[:limit]

    async def record_message(
        self,
        investigation_id: UUID,
        *,
        sender: str,
        body: str,
        mode: str | None = None,
    ) -> InvestigationMessageRecord:
        if investigation_id not in self.messages:
            self.messages[investigation_id] = []

        self._msg_id_counter += 1
        msg = InvestigationMessageRecord(
            id=self._msg_id_counter,
            investigation_id=investigation_id,
            sender=sender,
            body=body,
            mode=mode,
            created_at=datetime.now(timezone.utc),
        )
        self.messages[investigation_id].append(msg)
        return msg

    async def get_messages(
        self,
        investigation_id: UUID,
        *,
        after_id: int = 0,
        limit: int = 50,
    ) -> list[InvestigationMessageRecord]:
        msg_list = self.messages.get(investigation_id, [])
        filtered = [m for m in msg_list if m.id > after_id]
        filtered.sort(key=lambda m: m.id)
        return filtered[:limit]

    async def save_summary(
        self,
        investigation_id: UUID,
        *,
        findings: str,
        next_step: str,
        evidence_refs: list[str | dict[str, Any]],
    ) -> InvestigationSummaryRecord:
        summary = InvestigationSummaryRecord(
            id=1,
            investigation_id=investigation_id,
            findings=findings,
            next_step=next_step,
            evidence_refs=evidence_refs,
            created_at=datetime.now(timezone.utc),
        )
        self.summaries[investigation_id] = summary
        return summary

    async def get_summary(
        self,
        investigation_id: UUID,
    ) -> InvestigationSummaryRecord | None:
        return self.summaries.get(investigation_id)

    async def find_stale_running(
        self,
        cutoff: datetime,
    ) -> list[InvestigationRecord]:
        stale: list[InvestigationRecord] = []
        for rec in self.investigations.values():
            if rec.status == "running":
                if rec.last_heartbeat_at is not None and rec.last_heartbeat_at < cutoff:
                    stale.append(rec)
                elif (
                    rec.last_heartbeat_at is None
                    and rec.started_at is not None
                    and rec.started_at < cutoff
                ):
                    stale.append(rec)
        return stale
