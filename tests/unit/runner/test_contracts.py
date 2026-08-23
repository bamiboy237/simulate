"""Unit tests for domain runner schemas, contracts, and FakeRunner."""

from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import SecretStr
from tests.fakes.runners import FakeRunner

from app.domain.runner.base import CloudRunner
from app.domain.runner.schemas import (
    AgentArtifactRef,
    ChatEnvelope,
    EnvironmentSliceRef,
    EventType,
    ResourceLimits,
    RunnerEvent,
    RunnerState,
    RunnerStatus,
    SandboxHandle,
    SandboxSpec,
)


def test_sandbox_spec_roundtrip_and_defaults() -> None:
    """Verify SandboxSpec creation and schema serialization roundtrip."""
    spec = SandboxSpec(
        spec_version="1.0",
        task_brief="# Task: Reproduce checkout refund failure",
        environment_slice=EnvironmentSliceRef(
            world_id="world-refunds",
            world_version="v1.2",
            slice_name="refund_under_500",
            fixture_bundle_ref="bundle:sha256:abc12345",
            provenance="human-approved",
        ),
        investigator=AgentArtifactRef(
            kind="prime_profile",
            digest_or_profile="prime-investigator@1.0.0",
        ),
        tested_agent=AgentArtifactRef(
            kind="oci_digest",
            digest_or_profile="sha256:fedcba9876543210",
            entrypoint="python -m agent.server",
        ),
        env={"LOG_LEVEL": "DEBUG"},
        secrets={"OPENAI_API_KEY": SecretStr("sk-live-secret-999")},
        callback_base_url="https://api.simulate.local",
        resource_limits=ResourceLimits(timeout_s=1800, cpus=2, memory_mib=4096),
    )

    dumped = spec.model_dump()
    assert dumped["spec_version"] == "1.0"
    assert dumped["environment_slice"]["world_id"] == "world-refunds"
    assert dumped["investigator"]["digest_or_profile"] == "prime-investigator@1.0.0"
    assert dumped["tested_agent"]["entrypoint"] == "python -m agent.server"
    assert dumped["resource_limits"]["timeout_s"] == 1800

    # Reconstruct from model_dump
    reconstructed = SandboxSpec.model_validate(dumped)
    assert reconstructed.spec_version == spec.spec_version
    assert reconstructed.environment_slice.slice_name == "refund_under_500"
    assert reconstructed.secrets["OPENAI_API_KEY"].get_secret_value() == "sk-live-secret-999"


def test_sandbox_handle_and_status_models() -> None:
    """Verify SandboxHandle and RunnerStatus model construction."""
    created_at = datetime.now(timezone.utc)
    handle = SandboxHandle(
        sandbox_id="sbx-test-99",
        provider="fake",
        created_at=created_at,
    )
    assert handle.sandbox_id == "sbx-test-99"
    assert handle.provider == "fake"
    assert handle.created_at == created_at

    status = RunnerStatus(
        sandbox_id="sbx-test-99",
        state=RunnerState.running,
        detail={"ip": "10.0.0.1"},
    )
    assert status.sandbox_id == "sbx-test-99"
    assert status.state == RunnerState.running
    assert status.detail == {"ip": "10.0.0.1"}


def test_sandbox_spec_secret_redaction() -> None:
    """Verify secret values never appear in repr, str, or JSON dumps."""
    secret_val = "sk-extremely-sensitive-token-12345"
    spec = SandboxSpec(
        task_brief="Investigate payment error",
        environment_slice=EnvironmentSliceRef(
            world_id="w1",
            world_version="1",
            slice_name="s1",
            fixture_bundle_ref="f1",
            provenance="observed",
        ),
        investigator=AgentArtifactRef(
            kind="prime_profile",
            digest_or_profile="profile@1",
        ),
        callback_base_url="https://api.test",
        secrets={"ANTHROPIC_API_KEY": SecretStr(secret_val)},
    )

    # Raw secret must NOT be in string representations or JSON dumps
    assert secret_val not in str(spec)
    assert secret_val not in repr(spec)
    assert secret_val not in spec.model_dump_json()

    # In model_dump, the value is wrapped as SecretStr whose repr masks value
    dumped_secrets = spec.model_dump()["secrets"]
    assert secret_val not in str(dumped_secrets)

    # Secret can only be retrieved explicitly via get_secret_value()
    assert spec.secrets["ANTHROPIC_API_KEY"].get_secret_value() == secret_val


def test_event_types_and_runner_event_sequence_ordering() -> None:
    """Verify all event types and monotonic sequence ordering."""
    investigation_id = uuid4()
    now = datetime.now(timezone.utc)

    events = [
        RunnerEvent(
            investigation_id=investigation_id,
            seq=3,
            type=EventType.tool_call,
            payload={"tool": "trace.read", "args": {"trace_id": "tr_1"}},
            emitted_at=now,
        ),
        RunnerEvent(
            investigation_id=investigation_id,
            seq=1,
            type=EventType.investigation_started,
            payload={"brief_length": 120},
            emitted_at=now,
        ),
        RunnerEvent(
            investigation_id=investigation_id,
            seq=2,
            type=EventType.agent_ready,
            payload={},
            emitted_at=now,
        ),
        RunnerEvent(
            investigation_id=investigation_id,
            seq=4,
            type=EventType.summary_submitted,
            payload={"findings": "Root cause identified"},
            emitted_at=now,
        ),
    ]

    ordered = sorted(events, key=lambda e: e.seq)
    assert [e.seq for e in ordered] == [1, 2, 3, 4]
    assert ordered[0].type == EventType.investigation_started
    assert ordered[-1].type == EventType.summary_submitted

    # Check all EventType enum members are valid
    all_types = {
        EventType.investigation_started,
        EventType.agent_ready,
        EventType.thought,
        EventType.tool_call,
        EventType.tool_result,
        EventType.message,
        EventType.state_diff,
        EventType.evaluator_verdict,
        EventType.chat_from_user,
        EventType.chat_ack,
        EventType.summary_submitted,
        EventType.warning,
        EventType.error,
        EventType.heartbeat,
        EventType.investigation_finished,
    }
    assert len(all_types) == 15


def test_chat_envelope_modes() -> None:
    """Verify ChatEnvelope validation across supported modes."""
    msg_id = uuid4()

    for mode in ["prompt", "steer", "follow_up"]:
        envelope = ChatEnvelope(message_id=msg_id, body="Check user ID 42", mode=mode)  # type: ignore[arg-type]
        assert envelope.mode == mode

    with pytest.raises(Exception):
        ChatEnvelope(message_id=msg_id, body="Invalid", mode="unsupported_mode")  # type: ignore[arg-type]


def test_fake_runner_lifecycle_and_scripting() -> None:
    """Verify FakeRunner implements CloudRunner and supports scriptable testing."""
    fake = FakeRunner()
    assert isinstance(fake, CloudRunner)

    spec = SandboxSpec(
        task_brief="Test brief",
        environment_slice=EnvironmentSliceRef(
            world_id="w1",
            world_version="1",
            slice_name="s1",
            fixture_bundle_ref="b1",
            provenance="generated",
        ),
        investigator=AgentArtifactRef(kind="prime_profile", digest_or_profile="prof1"),
        callback_base_url="https://api.test",
    )

    fake.set_next_sandbox_id("sbx-custom-123")
    handle = fake.create_sandbox(spec)

    assert handle.sandbox_id == "sbx-custom-123"
    assert handle.provider == "fake"
    assert len(fake.specs) == 1
    assert fake.specs[0] == spec

    # Initial status is running
    status = fake.get_status(handle)
    assert status.sandbox_id == "sbx-custom-123"
    assert status.state == RunnerState.running

    # Set custom status
    fake.set_status("sbx-custom-123", RunnerState.completed, exit_code=0)
    status_updated = fake.get_status(handle)
    assert status_updated.state == RunnerState.completed
    assert status_updated.exit_code == 0

    # Terminate
    fake.terminate(handle)
    status_term = fake.get_status(handle)
    assert status_term.state == RunnerState.terminated
    assert "sbx-custom-123" in fake.terminated_sandboxes

    # Terminating twice is safe and idempotent
    fake.terminate(handle)
    assert fake.terminated_sandboxes.count("sbx-custom-123") == 1

    # Reset
    fake.reset()
    assert len(fake.specs) == 0
    assert len(fake.handles) == 0
    assert len(fake.terminated_sandboxes) == 0


def test_no_modal_in_domain_runner_except_client_wrapper() -> None:
    """Verify modal SDK imports are strictly isolated to _modal_client.py."""
    runner_dir = Path(__file__).parents[3] / "src" / "app" / "domain" / "runner"
    assert runner_dir.exists()
    for py_file in runner_dir.glob("*.py"):
        if py_file.name == "_modal_client.py":
            continue
        content = py_file.read_text()
        assert "import modal" not in content, f"{py_file.name} directly imports modal!"
        assert "from modal" not in content, f"{py_file.name} directly imports from modal!"
