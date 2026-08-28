"""Unit tests for ModalRunner adapter with mocked _modal_client."""

from unittest.mock import MagicMock, patch
from uuid import uuid4

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
        investigation_id=uuid4(),
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
            "BRIDGE_TOKEN": SecretStr("bridge-tok-secret-xyz"),
            "OPENAI_API_KEY": SecretStr("sk-openai-super-secret-key"),
            "MODAL_SECRET_TOKEN": SecretStr("mod-tok-secret-xyz"),
        },
        trace_ref={"trace_id": "tr-sample-1", "source": "langsmith"},
        callback_base_url="https://api.simulate.local/internal",
        resource_limits=ResourceLimits(timeout_s=1200, cpus=2, memory_mib=4096),
        outbound_domain_allowlist=["api.openai.com", "api.simulate.local"],
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

    assert kwargs["env"]["INVESTIGATION_ID"] == str(spec.investigation_id)
    assert kwargs["env"]["TASK_BRIEF"] == spec.task_brief
    assert kwargs["env"]["CONTROL_PLANE_CALLBACK_URL"] == "https://api.simulate.local/internal"
    assert kwargs["env"]["WORLD_GATEWAY_URL"] == "http://127.0.0.1:8001"
    assert kwargs["env"]["WORLD_ID"] == "support_world_v1"
    assert kwargs["env"]["SLICE_NAME"] == "order_cancellation"
    assert kwargs["env"]["INVESTIGATOR_REF"] == "prime-investigator@0.2.0"
    assert kwargs["env"]["INVESTIGATOR_ENTRYPOINT"] == "app.agent.run"
    assert kwargs["env"]["TESTED_AGENT_REF"] == "sha256:1122334455667788"
    assert kwargs["env"]["CUSTOM_VAR"] == "val123"
    # Truthful trace reference metadata is passed, never a fabricated trace.
    assert kwargs["env"]["TRACE_REF"] == '{"trace_id": "tr-sample-1", "source": "langsmith"}'
    # Resolved watchdog budget reaches the container so the bridge can fire
    # below the Modal outer timeout (validated against timeout_s=1200).
    assert kwargs["env"]["TOOL_TIMEOUT_S"] == "600"
    assert kwargs["env"]["TOOL_RECOVERY_TIMEOUT_S"] == "300"
    # The resolved outer lifetime drives the bridge's overall run cutoff.
    assert kwargs["env"]["SANDBOX_TIMEOUT_S"] == "1200"

    # Verify BRIDGE_TOKEN is NOT passed in env
    assert "BRIDGE_TOKEN" not in kwargs["env"]

    # Verify secrets were passed in plain form to client SDK call
    assert kwargs["secrets"]["BRIDGE_TOKEN"] == "bridge-tok-secret-xyz"
    assert kwargs["secrets"]["OPENAI_API_KEY"] == "sk-openai-super-secret-key"
    assert kwargs["secrets"]["MODAL_SECRET_TOKEN"] == "mod-tok-secret-xyz"

    # Verify resource limits
    assert kwargs["timeout_s"] == 1200
    assert kwargs["cpus"] == 2.0
    assert kwargs["memory_mib"] == 4096

    # Verify outbound domain allowlist
    assert kwargs["outbound_domain_allowlist"] == ["api.openai.com", "api.simulate.local"]

    # Verify spec itself masks the secrets
    assert "sk-openai-super-secret-key" not in spec.model_dump_json()
    assert "sk-openai-super-secret-key" not in str(spec)
    assert "bridge-tok-secret-xyz" not in spec.model_dump_json()
    assert "bridge-tok-secret-xyz" not in str(spec)


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


def test_modal_runner_rejects_missing_or_empty_bridge_token() -> None:
    """Verify ModalRunner rejects specs without a valid non-empty BRIDGE_TOKEN secret."""
    import pytest

    runner = ModalRunner()

    # Missing BRIDGE_TOKEN
    spec_missing = _sample_spec()
    del spec_missing.secrets["BRIDGE_TOKEN"]
    with pytest.raises(ValueError, match="BRIDGE_TOKEN"):
        runner.create_sandbox(spec_missing)

    # Empty BRIDGE_TOKEN
    spec_empty = _sample_spec()
    spec_empty.secrets["BRIDGE_TOKEN"] = SecretStr("")
    with pytest.raises(ValueError, match="BRIDGE_TOKEN"):
        runner.create_sandbox(spec_empty)


def test_build_investigation_image_discovery_directory() -> None:
    """Verify image recipe installs pinned tools and copies the extension."""
    from app.domain.runner._modal_client import build_investigation_image

    with patch("modal.Image.debian_slim") as mock_debian:
        mock_img = MagicMock()
        mock_debian.return_value = mock_img
        mock_img.apt_install.return_value = mock_img
        mock_img.run_commands.return_value = mock_img
        mock_img.pip_install.return_value = mock_img
        mock_img.add_local_file.return_value = mock_img
        mock_img.add_local_python_source.return_value = mock_img
        mock_img.env.return_value = mock_img

        build_investigation_image()

        commands = [arg for call in mock_img.run_commands.call_args_list for arg in call[0]]
        assert any("mkdir -p /root/.prime/agent/extensions" in cmd for cmd in commands)
        assert any("PRIME_AGENT_VERSION=0.8.1" in cmd for cmd in commands)
        assert any("setup_22.x" in cmd for cmd in commands)
        assert any("n >= 8" in cmd or "22.8" in cmd for cmd in commands)
        assert any("node --version && npm --version" in cmd for cmd in commands)
        assert any("prime-agent --version" in cmd for cmd in commands)
        pip_packages = [arg for call in mock_img.pip_install.call_args_list for arg in call[0]]
        assert "uvicorn>=0.34.0" in pip_packages

        # World Gateway extension is copied to Prime Agent's user extension directory.
        mock_img.add_local_file.assert_called_once_with(
            "src/app/domain/agent_runner/extensions/world_gateway.ts",
            "/root/.prime/agent/extensions/world_gateway.ts",
        )
