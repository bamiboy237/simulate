"""Unit tests for InvestigationService methods, tokens, idempotent events, and stale sweep."""

from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from pydantic import SecretStr
from tests.fakes.investigation_repository import InMemoryInvestigationRepository
from tests.fakes.runners import FakeRunner

from app.config import Settings
from app.domain.investigation.errors import InvalidBridgeTokenError
from app.domain.investigation.schemas import (
    InvestigationCreateRequest,
    InvestigationStatus,
)
from app.domain.investigation.service import InvestigationService
from app.domain.runner.modal_runner import ModalRunner
from app.domain.runner.schemas import (
    AgentArtifactRef,
    EnvironmentSliceRef,
    EventType,
    ResourceLimits,
    RunnerEvent,
    SandboxHandle,
    SandboxSpec,
)


def _sample_create_request() -> InvestigationCreateRequest:
    return InvestigationCreateRequest(
        trace_ref={"platform": "langsmith", "trace_id": "tr_200"},
        world_ref={"world_id": "support_v1"},
        slice_ref=EnvironmentSliceRef(
            world_id="support_v1",
            world_version="1.0",
            slice_name="auth_failure",
            fixture_bundle_ref="bundle_2",
            provenance="observed",
        ),
        investigator_ref=AgentArtifactRef(
            kind="prime_profile",
            digest_or_profile="prime@0.2.0",
        ),
    )


def _modal_settings(
    *,
    allowlist: list[str],
    base_url: str | None = None,
    provider: str = "openai",
    tool_timeout_s: int | None = None,
    tool_recovery_timeout_s: int | None = None,
) -> Settings:
    kwargs: dict[str, Any] = dict(
        database_url="postgresql://user:pass@localhost:5432/test",
        environment="test",
        modal_enabled=False,
        model_provider=provider,  # type: ignore[arg-type]
        model_name="test-model",
        model_api_key=SecretStr("test-key"),
        model_base_url=base_url,
        modal_outbound_domain_allowlist=allowlist,
        _env_file=None,
    )
    if tool_timeout_s is not None:
        kwargs["tool_timeout_s"] = tool_timeout_s
    if tool_recovery_timeout_s is not None:
        kwargs["tool_recovery_timeout_s"] = tool_recovery_timeout_s
    return Settings(**kwargs)


class CapturingModalRunner(ModalRunner):
    """Record the SandboxSpec passed to create_sandbox without provisioning."""

    def __init__(self) -> None:
        super().__init__()
        self.specs: list[SandboxSpec] = []

    def create_sandbox(self, spec: SandboxSpec) -> SandboxHandle:
        self.specs.append(spec)
        return SandboxHandle(sandbox_id="sbx-captured", provider="modal")


@pytest.mark.asyncio
async def test_token_generation_and_verification() -> None:
    """Verify bearer tokens verify correctly and bad tokens are rejected."""
    repo = InMemoryInvestigationRepository()
    service = InvestigationService(repo, runner=FakeRunner())

    resp, token = await service.start(_sample_create_request())
    assert token
    assert resp.bridge_token == token

    # Verification succeeds with valid token
    rec = await service.verify_bridge_token(token)
    assert rec.id == resp.id

    # Invalid token raises error
    with pytest.raises(InvalidBridgeTokenError):
        await service.verify_bridge_token("invalid_token_string")


@pytest.mark.asyncio
async def test_idempotent_event_ingestion_and_heartbeat() -> None:
    """Verify events can be posted repeatedly without duplicating and update heartbeats."""
    repo = InMemoryInvestigationRepository()
    service = InvestigationService(repo, runner=FakeRunner())

    resp, token = await service.start(_sample_create_request())
    await service.transition_status(resp.id, InvestigationStatus.PROVISIONING)
    await service.transition_status(resp.id, InvestigationStatus.RUNNING)

    now = datetime.now(timezone.utc)
    events = [
        RunnerEvent(
            investigation_id=resp.id,
            seq=1,
            type=EventType.investigation_started,
            payload={},
            emitted_at=now,
        ),
        RunnerEvent(
            investigation_id=resp.id,
            seq=2,
            type=EventType.heartbeat,
            payload={},
            emitted_at=now,
        ),
    ]

    # First ingest
    inserted1 = await service.record_events(token, events)
    assert inserted1 == 2

    # Second ingest of identical batch is idempotent (0 new inserts)
    inserted2 = await service.record_events(token, events)
    assert inserted2 == 0

    # Heartbeat was recorded
    detail = await service.get_by_id(resp.id)
    assert detail.last_heartbeat_at is not None


@pytest.mark.asyncio
async def test_summary_submitted_waits_for_investigation_finished() -> None:
    """Store the summary without completing before the bridge emits agent_end."""
    repo = InMemoryInvestigationRepository()
    service = InvestigationService(repo, runner=FakeRunner())

    resp, token = await service.start(_sample_create_request())
    await service.transition_status(resp.id, InvestigationStatus.PROVISIONING)
    await service.transition_status(resp.id, InvestigationStatus.RUNNING)

    summary_event = RunnerEvent(
        investigation_id=resp.id,
        seq=1,
        type=EventType.summary_submitted,
        payload={
            "findings": "Missing idempotency key in refund tool",
            "next_step": "Add request ID parameter to payment endpoint",
            "evidence_refs": [{"event_id": "ev_1"}],
        },
        emitted_at=datetime.now(timezone.utc),
    )

    await service.record_events(token, [summary_event])

    detail = await service.get_by_id(resp.id)
    assert detail.status == InvestigationStatus.RUNNING
    assert detail.summary is not None
    assert detail.summary.findings == "Missing idempotency key in refund tool"
    assert detail.summary.next_step == "Add request ID parameter to payment endpoint"

    finished_event = RunnerEvent(
        investigation_id=resp.id,
        seq=2,
        type=EventType.investigation_finished,
        payload={},
        emitted_at=datetime.now(timezone.utc),
    )
    await service.record_events(token, [finished_event])

    detail = await service.get_by_id(resp.id)
    assert detail.status == InvestigationStatus.COMPLETED


@pytest.mark.asyncio
async def test_final_events_after_completed_persist_but_general_writes_stay_closed() -> None:
    """Verify closing bridge events persist after COMPLETED without reopening writes."""
    repo = InMemoryInvestigationRepository()
    service = InvestigationService(repo, runner=FakeRunner())

    resp, token = await service.start(_sample_create_request())
    await service.transition_status(resp.id, InvestigationStatus.PROVISIONING)
    await service.transition_status(resp.id, InvestigationStatus.RUNNING)

    now = datetime.now(timezone.utc)

    def ev(seq: int, type_: EventType) -> RunnerEvent:
        return RunnerEvent(
            investigation_id=resp.id,
            seq=seq,
            type=type_,
            payload={},
            emitted_at=now,
        )

    # The same accepted batch holds summary plus agent_end: the whole batch is
    # persisted before the summary transitions the investigation to COMPLETED.
    inserted_batch = await service.record_events(
        token,
        [
            ev(1, EventType.summary_submitted),
            ev(2, EventType.investigation_finished),
        ],
    )
    assert inserted_batch == 2

    detail = await service.get_by_id(resp.id)
    assert detail.status == InvestigationStatus.COMPLETED
    persisted = await repo.get_events(resp.id, after_seq=0)
    assert [e.seq for e in persisted] == [1, 2]

    # The immediately following final batch (agent_end flushed after the summary
    # batch) must still persist even though status is now COMPLETED.
    inserted_tail = await service.record_events(
        token,
        [ev(3, EventType.investigation_finished)],
    )
    assert inserted_tail == 1

    # A trailing error event from the active bridge also persists.
    inserted_err = await service.record_events(token, [ev(4, EventType.error)])
    assert inserted_err == 1

    # General writes to a completed investigation remain rejected.
    inserted_msg = await service.record_events(token, [ev(5, EventType.message)])
    assert inserted_msg == 0

    persisted_after = await repo.get_events(resp.id, after_seq=0)
    assert [e.seq for e in persisted_after] == [1, 2, 3, 4]

    # Re-posting a closing event is idempotent.
    assert (
        await service.record_events(
            token,
            [ev(3, EventType.investigation_finished)],
        )
        == 0
    )


@pytest.mark.asyncio
async def test_mixed_tail_batch_persists_before_completion() -> None:
    """Persist post-summary tail events and agent_end before closing the investigation."""
    repo = InMemoryInvestigationRepository()
    service = InvestigationService(repo, runner=FakeRunner())

    resp, token = await service.start(_sample_create_request())
    await service.transition_status(resp.id, InvestigationStatus.PROVISIONING)
    await service.transition_status(resp.id, InvestigationStatus.RUNNING)
    now = datetime.now(timezone.utc)

    summary = RunnerEvent(
        investigation_id=resp.id,
        seq=1,
        type=EventType.summary_submitted,
        payload={"findings": "F", "next_step": "N", "evidence_refs": ["ev_1"]},
        emitted_at=now,
    )
    await service.record_events(token, [summary])
    assert (await service.get_by_id(resp.id)).status == InvestigationStatus.RUNNING

    tail = [
        RunnerEvent(
            investigation_id=resp.id,
            seq=2,
            type=EventType.message,
            payload={"text": "final response"},
            emitted_at=now,
        ),
        RunnerEvent(
            investigation_id=resp.id,
            seq=3,
            type=EventType.investigation_finished,
            payload={},
            emitted_at=now,
        ),
    ]
    assert await service.record_events(token, tail) == 2

    detail = await service.get_by_id(resp.id)
    assert detail.status == InvestigationStatus.COMPLETED
    persisted = await repo.get_events(resp.id, after_seq=0)
    assert [(event.seq, event.type) for event in persisted] == [
        (1, EventType.summary_submitted.value),
        (2, EventType.message.value),
        (3, EventType.investigation_finished.value),
    ]


@pytest.mark.asyncio
async def test_stale_investigation_sweep() -> None:
    """Verify sweep_stale transitions inactive running investigations to failed."""
    repo = InMemoryInvestigationRepository()
    service = InvestigationService(repo, runner=FakeRunner())

    resp, _ = await service.start(_sample_create_request())
    await service.transition_status(resp.id, InvestigationStatus.PROVISIONING)
    await service.transition_status(resp.id, InvestigationStatus.RUNNING)

    # Fake silence: last heartbeat 120s ago
    past = datetime.now(timezone.utc) - timedelta(seconds=120)
    await repo.update_heartbeat(resp.id, past)

    reconciled = await service.sweep_stale(silence_seconds=90)
    assert resp.id in reconciled

    detail = await service.get_by_id(resp.id)
    assert detail.status == InvestigationStatus.FAILED
    assert "heartbeat silence" in (detail.error or "")


@pytest.mark.asyncio
async def test_start_rejects_reserved_keys_before_persistence() -> None:
    """Verify start() rejects reserved env/secret keys before persisting any record."""
    repo = InMemoryInvestigationRepository()
    service = InvestigationService(repo, runner=FakeRunner())

    # 1. Reserved environment key rejected before record is created
    req_env = InvestigationCreateRequest(
        trace_ref={"platform": "langsmith", "trace_id": "tr_200"},
        world_ref={"world_id": "support_v1"},
        slice_ref=EnvironmentSliceRef(
            world_id="support_v1",
            world_version="1.0",
            slice_name="auth_failure",
            fixture_bundle_ref="bundle_2",
            provenance="observed",
        ),
        investigator_ref=AgentArtifactRef(
            kind="prime_profile",
            digest_or_profile="prime@0.2.0",
        ),
        env={"INVESTIGATION_ID": "override-123"},
    )

    with pytest.raises(ValueError, match="Caller cannot override reserved environment variables"):
        await service.start(req_env)

    # Prove repository remains completely empty (0 records persisted)
    assert len(repo.investigations) == 0

    # 2. Reserved secret key rejected before record is created
    req_secret = InvestigationCreateRequest(
        trace_ref={"platform": "langsmith", "trace_id": "tr_200"},
        world_ref={"world_id": "support_v1"},
        slice_ref=EnvironmentSliceRef(
            world_id="support_v1",
            world_version="1.0",
            slice_name="auth_failure",
            fixture_bundle_ref="bundle_2",
            provenance="observed",
        ),
        investigator_ref=AgentArtifactRef(
            kind="prime_profile",
            digest_or_profile="prime@0.2.0",
        ),
        secrets={"BRIDGE_TOKEN": SecretStr("caller-supplied-token")},
    )

    with pytest.raises(ValueError, match="Caller cannot override reserved secret keys"):
        await service.start(req_secret)

    # Prove repository is still completely empty
    assert len(repo.investigations) == 0


@pytest.mark.asyncio
async def test_modal_start_rejects_missing_or_loopback_control_plane_url_before_persistence() -> (
    None
):
    """Verify Modal launches require a public CONTROL_PLANE_PUBLIC_URL before persistence."""
    repo = InMemoryInvestigationRepository()

    # 1. Missing URL
    service = InvestigationService(repo, runner=CapturingModalRunner())
    with pytest.raises(ValueError, match="CONTROL_PLANE_PUBLIC_URL is required"):
        await service.start(_sample_create_request())
    assert len(repo.investigations) == 0

    # 2. Loopback URL is never used as a Modal callback
    service_loopback = InvestigationService(
        repo,
        runner=CapturingModalRunner(),
        control_plane_url="http://127.0.0.1:8000",
    )
    with pytest.raises(ValueError, match="public URL"):
        await service_loopback.start(_sample_create_request())
    assert len(repo.investigations) == 0

    # 3. Non-HTTP(S) scheme
    service_ftp = InvestigationService(
        repo,
        runner=CapturingModalRunner(),
        control_plane_url="ftp://api.simulate.local",
    )
    with pytest.raises(ValueError, match="http or https"):
        await service_ftp.start(_sample_create_request())
    assert len(repo.investigations) == 0


@pytest.mark.asyncio
async def test_modal_start_rejects_malformed_or_incomplete_allowlist_before_persistence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify the outbound allowlist is validated and must cover both mandatory hosts."""
    repo = InMemoryInvestigationRepository()
    service = InvestigationService(
        repo,
        runner=CapturingModalRunner(),
        control_plane_url="https://api.simulate.local",
    )

    # 1. Empty allowlist
    monkeypatch.setattr(
        "app.domain.investigation.service.get_settings",
        lambda: _modal_settings(allowlist=[]),
    )
    with pytest.raises(ValueError, match="MODAL_OUTBOUND_DOMAIN_ALLOWLIST must be configured"):
        await service.start(_sample_create_request())
    assert len(repo.investigations) == 0

    # 2. Missing the mandatory control-plane and model-provider hostnames
    monkeypatch.setattr(
        "app.domain.investigation.service.get_settings",
        lambda: _modal_settings(allowlist=["pypi.org"]),
    )
    with pytest.raises(ValueError, match="must include the control-plane and model-provider"):
        await service.start(_sample_create_request())
    assert len(repo.investigations) == 0

    # 3. Scheme, port, and underscore entries are not bare domains
    for bad_entry in (
        "https://api.simulate.local",
        "api.openai.com:443",
        "my_proxy.example.com",
    ):
        monkeypatch.setattr(
            "app.domain.investigation.service.get_settings",
            lambda: _modal_settings(allowlist=["api.simulate.local", "api.openai.com", bad_entry]),
        )
        with pytest.raises(ValueError, match="bare domain"):
            await service.start(_sample_create_request())
        assert len(repo.investigations) == 0


@pytest.mark.asyncio
async def test_modal_start_derives_model_host_from_openai_standard_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify OpenAI without MODEL_BASE_URL requires api.openai.com in the allowlist."""
    repo = InMemoryInvestigationRepository()
    service = InvestigationService(
        repo,
        runner=CapturingModalRunner(),
        control_plane_url="https://api.simulate.local",
    )

    # api.openai.com is the derived host; it is missing from the allowlist.
    monkeypatch.setattr(
        "app.domain.investigation.service.get_settings",
        lambda: _modal_settings(allowlist=["api.simulate.local"]),
    )
    with pytest.raises(ValueError, match="api.openai.com"):
        await service.start(_sample_create_request())
    assert len(repo.investigations) == 0


@pytest.mark.asyncio
async def test_modal_start_derives_model_host_from_custom_base_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify a custom MODEL_BASE_URL supplies the required model-provider host."""
    repo = InMemoryInvestigationRepository()
    service = InvestigationService(
        repo,
        runner=CapturingModalRunner(),
        control_plane_url="https://api.simulate.local",
    )

    monkeypatch.setattr(
        "app.domain.investigation.service.get_settings",
        lambda: _modal_settings(
            allowlist=["api.simulate.local"],
            base_url="https://proxy.example.com/v1",
        ),
    )
    with pytest.raises(ValueError, match="proxy.example.com"):
        await service.start(_sample_create_request())
    assert len(repo.investigations) == 0

    # With the derived host present the launch proceeds to provisioning.
    monkeypatch.setattr(
        "app.domain.investigation.service.get_settings",
        lambda: _modal_settings(
            allowlist=["api.simulate.local", "proxy.example.com"],
            base_url="https://proxy.example.com/v1",
        ),
    )
    resp, token = await service.start(_sample_create_request())
    assert resp.status == InvestigationStatus.PENDING
    assert token
    assert len(repo.investigations) == 1


@pytest.mark.asyncio
async def test_modal_start_fails_when_model_host_cannot_be_derived(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify an underviable provider hostname fails with a concise config error."""
    repo = InMemoryInvestigationRepository()
    service = InvestigationService(
        repo,
        runner=CapturingModalRunner(),
        control_plane_url="https://api.simulate.local",
    )

    # Anthropic without MODEL_BASE_URL has no derivable hostname.
    monkeypatch.setattr(
        "app.domain.investigation.service.get_settings",
        lambda: _modal_settings(
            allowlist=["api.simulate.local", "api.anthropic.com"],
            provider="anthropic",
        ),
    )
    with pytest.raises(ValueError, match="Cannot derive the model-provider hostname"):
        await service.start(_sample_create_request())
    assert len(repo.investigations) == 0


@pytest.mark.asyncio
async def test_modal_start_passes_validated_allowlist_into_spec(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify the validated allowlist and public callback reach the SandboxSpec."""
    repo = InMemoryInvestigationRepository()
    runner = CapturingModalRunner()
    service = InvestigationService(
        repo,
        runner=runner,
        control_plane_url="https://api.simulate.local",
    )

    monkeypatch.setattr(
        "app.domain.investigation.service.get_settings",
        lambda: _modal_settings(
            allowlist=["API.SIMULATE.LOCAL", "api.openai.com", "pypi.org"],
        ),
    )
    resp, token = await service.start(_sample_create_request())

    assert resp.status == InvestigationStatus.PENDING
    assert token
    assert len(runner.specs) == 1
    spec = runner.specs[0]
    assert spec.callback_base_url == "https://api.simulate.local"
    # Entries are normalized to lowercase bare domains and passed explicitly.
    assert spec.outbound_domain_allowlist == [
        "api.simulate.local",
        "api.openai.com",
        "pypi.org",
    ]
    assert spec.secrets["OPENAI_API_KEY"].get_secret_value() == "test-key"
    assert spec.investigation_id == resp.id
    # Truthful trace reference metadata reaches the sandbox spec.
    assert spec.trace_ref == {"platform": "langsmith", "trace_id": "tr_200"}


@pytest.mark.asyncio
async def test_modal_start_spec_carries_settings_watchdog_timeouts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify Settings -> SandboxSpec -> sandbox env watchdog configuration."""
    repo = InMemoryInvestigationRepository()
    runner = CapturingModalRunner()
    service = InvestigationService(
        repo,
        runner=runner,
        control_plane_url="https://api.simulate.local",
    )

    monkeypatch.setattr(
        "app.domain.investigation.service.get_settings",
        lambda: _modal_settings(
            allowlist=["api.simulate.local", "api.openai.com"],
            tool_timeout_s=120,
            tool_recovery_timeout_s=60,
        ),
    )
    resp, _token = await service.start(
        _sample_create_request().model_copy(
            update={"resource_limits": ResourceLimits(timeout_s=300)}
        )
    )
    assert resp.status == InvestigationStatus.PENDING
    spec = runner.specs[0]
    assert spec.resource_limits.timeout_s == 300
    assert spec.tool_timeout_s == 120
    assert spec.tool_recovery_timeout_s == 60


@pytest.mark.asyncio
async def test_modal_start_rejects_watchdog_budget_exceeding_sandbox_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify a 300s sandbox with default 600/300 watchdog settings is rejected.

    This is the exact live-repro shape: the watchdog would never fire before
    Modal killed the 300s container, so the launch must fail without starting.
    """
    repo = InMemoryInvestigationRepository()
    runner = CapturingModalRunner()
    service = InvestigationService(
        repo,
        runner=runner,
        control_plane_url="https://api.simulate.local",
    )

    monkeypatch.setattr(
        "app.domain.investigation.service.get_settings",
        lambda: _modal_settings(allowlist=["api.simulate.local", "api.openai.com"]),
    )
    with pytest.raises(ValueError, match="watchdog budget exceeds"):
        await service.start(
            _sample_create_request().model_copy(
                update={"resource_limits": ResourceLimits(timeout_s=300)}
            )
        )
    assert len(repo.investigations) == 0


def test_resolved_modal_runner_gets_default_allowlist(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify _resolve_runner forwards the configured allowlist to ModalRunner."""
    repo = InMemoryInvestigationRepository()
    settings = _modal_settings(allowlist=["api.simulate.local", "api.openai.com"])
    settings.modal_enabled = True
    monkeypatch.setattr("app.domain.investigation.service.get_settings", lambda: settings)

    service = InvestigationService(repo)
    assert isinstance(service._runner, ModalRunner)
    assert service._runner.default_outbound_domain_allowlist == [
        "api.simulate.local",
        "api.openai.com",
    ]
