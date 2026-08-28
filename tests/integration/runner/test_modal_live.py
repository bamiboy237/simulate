"""Integration test for live Modal sandbox execution.

Gated strictly on settings.modal_enabled so it skips cleanly in offline environments.
"""

from uuid import uuid4

import pytest
from pydantic import SecretStr

from app.config import get_settings
from app.domain.runner import (
    AgentArtifactRef,
    EnvironmentSliceRef,
    ModalRunner,
    ResourceLimits,
    RunnerState,
    SandboxSpec,
)


@pytest.mark.live
def test_modal_live_sandbox_lifecycle() -> None:
    """Create, verify status, and terminate a live Modal sandbox when enabled."""
    try:
        settings = get_settings()
    except Exception:
        pytest.skip("Settings unavailable for live Modal test.")

    if not settings.modal_enabled:
        pytest.skip("MODAL_ENABLED is false; skipping live Modal runner integration test.")

    runner = ModalRunner(
        app_name=settings.modal_app_name,
        default_outbound_domain_allowlist=settings.modal_outbound_domain_allowlist,
    )
    spec = SandboxSpec(
        spec_version="1.0.0",
        investigation_id=uuid4(),
        task_brief="# Live integration test brief",
        environment_slice=EnvironmentSliceRef(
            world_id="live_test_world",
            world_version="1.0",
            slice_name="test_slice",
            fixture_bundle_ref="test_bundle",
            provenance="observed",
        ),
        investigator=AgentArtifactRef(
            kind="prime_profile",
            digest_or_profile="prime@0.2.0",
            entrypoint="python -c \"print('live test')\"",
        ),
        secrets={
            "BRIDGE_TOKEN": SecretStr("live-test-bridge-token"),
            "TEST_KEY": SecretStr("test_secret_value"),
        },
        callback_base_url="https://api.simulate.local",
        resource_limits=ResourceLimits(timeout_s=300, cpus=1, memory_mib=1024),
        # Fit the watchdog budget inside the 300s outer timeout.
        tool_timeout_s=120,
        tool_recovery_timeout_s=60,
        outbound_domain_allowlist=["api.simulate.local", "api.openai.com"],
    )

    handle = runner.create_sandbox(spec)
    assert handle.sandbox_id
    assert handle.provider == "modal"

    status = runner.get_status(handle)
    assert status.state in {RunnerState.running, RunnerState.completed}

    runner.terminate(handle)
    runner.terminate(handle)  # Test safe repeated termination
