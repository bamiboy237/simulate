"""Unit tests for ModalRunner adapter with mocked _modal_client."""

from unittest.mock import MagicMock, patch

from pydantic import SecretStr

from app.domain.runner.modal_runner import ModalRunner
from app.domain.runner.schemas import (
    AgentArtifactRef,
    EnvironmentSliceRef,
    ResourceLimits,
    RunnerState,
    SandboxHandle,
    SandboxSpec,
)


def _sample_spec() -> SandboxSpec:
    return SandboxSpec(
        spec_version="1.0.0",
        task_brief="# Task brief: reproduce failure",
        environment_slice=EnvironmentSliceRef(
            world_id="support_world_v1",
            world_version="2.0",
            slice_name="order_cancellation",
            fixture_bundle_ref="bundles/orders.json",
            provenance="observed",
        ),
        investigator=AgentArtifactRef(
            kind="prime_profile",
            digest_or_profile="prime-investigator@0.2.0",
            entrypoint="app.agent.run",
        ),
        tested_agent=AgentArtifactRef(
            kind="oci_digest",
            digest_or_profile="sha256:1122334455667788",
            entrypoint="app.tested.run",
        ),
        env={"CUSTOM_VAR": "val123"},
        secrets={
            "OPENAI_API_KEY": SecretStr("sk-openai-super-secret-key"),
            "MODAL_SECRET_TOKEN": SecretStr("mod-tok-secret-xyz"),
        },
        callback_base_url="https://api.simulate.local/internal",
        resource_limits=ResourceLimits(timeout_s=1200, cpus=2, memory_mib=4096),
    )


@patch("app.domain.runner._modal_client.create_sandbox")
def test_modal_runner_create_sandbox(mock_create_sandbox: MagicMock) -> None:
    """Verify ModalRunner injects metadata, secrets, and environment correctly."""
    mock_create_sandbox.return_value = "sbx-modal-998877"

    runner = ModalRunner(app_name="test-sim-app")
    spec = _sample_spec()

    handle = runner.create_sandbox(spec)

    assert handle.sandbox_id == "sbx-modal-998877"
    assert handle.provider == "modal"

    mock_create_sandbox.assert_called_once()
    _, kwargs = mock_create_sandbox.call_args

    assert kwargs["env"]["CONTROL_PLANE_CALLBACK_URL"] == "https://api.simulate.local/internal"
    assert kwargs["env"]["WORLD_ID"] == "support_world_v1"
    assert kwargs["env"]["SLICE_NAME"] == "order_cancellation"
    assert kwargs["env"]["INVESTIGATOR_REF"] == "prime-investigator@0.2.0"
    assert kwargs["env"]["INVESTIGATOR_ENTRYPOINT"] == "app.agent.run"
    assert kwargs["env"]["TESTED_AGENT_REF"] == "sha256:1122334455667788"
    assert kwargs["env"]["CUSTOM_VAR"] == "val123"

    # Verify secrets were passed in plain form to client SDK call
    assert kwargs["secrets"]["OPENAI_API_KEY"] == "sk-openai-super-secret-key"
    assert kwargs["secrets"]["MODAL_SECRET_TOKEN"] == "mod-tok-secret-xyz"

    # Verify resource limits
    assert kwargs["timeout_s"] == 1200
    assert kwargs["cpus"] == 2.0
    assert kwargs["memory_mib"] == 4096

    # Verify spec itself masks the secrets
    assert "sk-openai-super-secret-key" not in spec.model_dump_json()
    assert "sk-openai-super-secret-key" not in str(spec)


@patch("app.domain.runner._modal_client.poll_sandbox")
def test_modal_runner_get_status(mock_poll: MagicMock) -> None:
    """Verify ModalRunner translates poll exit codes to RunnerStatus."""
    runner = ModalRunner()
    handle = SandboxHandle(sandbox_id="sbx-123", provider="modal")

    # Running: poll returns None
    mock_poll.return_value = None
    status = runner.get_status(handle)
    assert status.state == RunnerState.running

    # Completed: poll returns 0
    mock_poll.return_value = 0
    status = runner.get_status(handle)
    assert status.state == RunnerState.completed
    assert status.exit_code == 0

    # Failed: poll returns nonzero
    mock_poll.return_value = 137
    status = runner.get_status(handle)
    assert status.state == RunnerState.failed
    assert status.exit_code == 137


@patch("app.domain.runner._modal_client.terminate_sandbox")
def test_modal_runner_terminate_idempotent(mock_terminate: MagicMock) -> None:
    """Verify terminate calls client and is safe to invoke repeatedly."""
    runner = ModalRunner()
    handle = SandboxHandle(sandbox_id="sbx-456", provider="modal")

    runner.terminate(handle)
    runner.terminate(handle)

    assert mock_terminate.call_count == 2
    mock_terminate.assert_called_with("sbx-456")
