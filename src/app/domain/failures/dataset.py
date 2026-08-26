"""Load the versioned, deterministic Phase 6 labeled trace dataset."""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from app.adapters.sources.fixture_source import FixtureTraceSource
from app.domain.evidence.models import stable_evidence_id
from app.domain.evidence.schemas import (
    TraceEvent,
    TraceEventKind,
    TraceEvidence,
    TraceModelRef,
    TraceOutcome,
    TraceSourceRef,
    compute_content_hash,
)
from app.domain.failures.schemas import (
    FailureDataset,
    FailureDatasetManifest,
    TraceLabel,
    content_hash,
)

DATASET_ID = "failure_traces_v1"
DATASET_VERSION = 1
MANIFEST_PATH = Path(__file__).parent / "fixtures" / f"{DATASET_ID}.json"


def _synthetic_trace(trace_id: str, kind: str) -> TraceEvidence:
    """Build one intentionally hand-authored edge trace.

    These two traces cover provider-independent routing and generation failures
    that are not present in the accepted support fixtures.  They contain no
    user text and use the same canonical schema as imported traces.
    """
    start = datetime(2026, 8, 1, tzinfo=UTC)
    root = TraceEvent(
        event_id="root",
        kind=TraceEventKind.TURN,
        name="support_agent.turn",
        start_time=start,
        end_time=start + timedelta(milliseconds=100),
        duration_ms=100,
        attributes={"support.intent": "support", "support.message.length": 24},
    )
    if kind == "routing":
        event = TraceEvent(
            event_id="routing-1",
            kind=TraceEventKind.ROUTING,
            name="support_agent.routing.no_route",
            parent_id="root",
            start_time=start + timedelta(milliseconds=1),
            end_time=start + timedelta(milliseconds=20),
            duration_ms=19,
            error_code="no_route",
            attributes={"agent.routing.instructions.version": "routing-v1"},
        )
        outcome = TraceOutcome.BLOCKED
        reason = "routing_no_route"
    else:
        event = TraceEvent(
            event_id="answer-1",
            kind=TraceEventKind.ANSWER,
            name="support_agent.answer.invalid_output",
            parent_id="root",
            start_time=start + timedelta(milliseconds=1),
            end_time=start + timedelta(milliseconds=80),
            duration_ms=79,
            error_code="invalid_output",
            attributes={"agent.answer.instructions.version": "answer-v1"},
        )
        outcome = TraceOutcome.FAILED
        reason = "generation_invalid_output"
    source = TraceSourceRef(
        platform="fixture",
        project="failure-analysis",
        trace_id=trace_id,
        run_id=f"run-{trace_id}",
    )
    evidence = TraceEvidence(
        evidence_id=stable_evidence_id(source),
        source=source,
        scenario_id=trace_id,
        workflow="support",
        environment="fixture",
        workflow_version="phase6-v1",
        model=TraceModelRef(provider="fixture", name="deterministic"),
        outcome=outcome,
        reason_code=reason,
        events=[root, event],
    )
    return evidence.model_copy(update={"content_hash": compute_content_hash(evidence)})


def _load_manifest() -> FailureDatasetManifest:
    with MANIFEST_PATH.open(encoding="utf-8") as handle:
        payload = json.load(handle)
    return FailureDatasetManifest.model_validate(payload)


def load_failure_dataset() -> FailureDataset:
    """Return the complete canonical dataset in manifest order.

    Existing accepted fixtures are passed through ``FixtureTraceSource``. The
    small number of intentional edge cases are generated with the same
    canonical constructor on every invocation.
    """
    manifest = _load_manifest()
    source = FixtureTraceSource()
    traces: list[TraceEvidence] = []
    for label in manifest.labels:
        if label.trace_id.startswith("phase6-"):
            kind = "routing" if "routing" in label.trace_id else "generation"
            traces.append(_synthetic_trace(label.trace_id, kind))
        else:
            traces.append(source._evidence_for(label.trace_id))  # noqa: SLF001

    expected_payload = [
        {
            "trace_id": label.trace_id,
            "label": label.label.value if label.label is not None else None,
            "source": label.source,
            "trace_hash": compute_content_hash(trace),
        }
        for label, trace in zip(manifest.labels, traces, strict=True)
    ]
    expected_hash = content_hash(expected_payload)
    if manifest.content_hash != expected_hash:
        raise ValueError(
            f"dataset manifest {manifest.dataset_id!r} does not match regenerated content"
        )
    return FailureDataset(manifest=manifest, traces=tuple(traces))


def regenerate_manifest() -> dict[str, object]:
    """Return the manifest payload generated from current canonical fixtures."""
    source = FixtureTraceSource()
    labels = [TraceLabel.model_validate(item) for item in _load_manifest().labels]
    traces = [
        _synthetic_trace(item.trace_id, "routing" if "routing" in item.trace_id else "generation")
        if item.trace_id.startswith("phase6-")
        else source._evidence_for(item.trace_id)  # noqa: SLF001
        for item in labels
    ]
    payload = [
        {
            "trace_id": item.trace_id,
            "label": item.label.value if item.label is not None else None,
            "source": item.source,
            "trace_hash": compute_content_hash(trace),
        }
        for item, trace in zip(labels, traces, strict=True)
    ]
    return {
        "schema_version": "1.0.0",
        "dataset_id": DATASET_ID,
        "dataset_version": DATASET_VERSION,
        "trace_schema_version": "3.0.0",
        "labels": [item.model_dump(mode="json") for item in labels],
        "content_hash": content_hash(payload),
    }


__all__ = ["DATASET_ID", "DATASET_VERSION", "load_failure_dataset", "regenerate_manifest"]

