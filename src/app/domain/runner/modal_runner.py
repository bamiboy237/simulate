"""Modal implementation of the CloudRunner protocol for sandbox management."""

import json
from datetime import datetime, timezone

from app.domain.runner import _modal_client
from app.domain.runner.base import CloudRunner
from app.domain.runner.schemas import (
    RESERVED_ENV_KEYS,
    RunnerState,
    RunnerStatus,
    SandboxHandle,
    SandboxSpec,
)


class ModalRunner(CloudRunner):
    """CloudRunner implementation backed by Modal Linux sandboxes."""

    def __init__(
        self,
        app_name: str = "simulate-mvp",
        default_timeout_s: int = 3600,
        default_outbound_domain_allowlist: list[str] | None = None,
    ) -> None:
        self.app_name = app_name
        self.default_timeout_s = default_timeout_s
        self.default_outbound_domain_allowlist = default_outbound_domain_allowlist or []

    def create_sandbox(self, spec: SandboxSpec) -> SandboxHandle:
        """Launch a detached Modal container for the given SandboxSpec."""
        env_vars: dict[str, str | None] = {
            "INVESTIGATION_ID": str(spec.investigation_id),
            "TASK_BRIEF": spec.task_brief,
            "CONTROL_PLANE_CALLBACK_URL": spec.callback_base_url,
            "WORLD_GATEWAY_URL": "http://127.0.0.1:8001",
            "SPEC_VERSION": spec.spec_version,
            "WORLD_ID": spec.environment_slice.world_id,
            "WORLD_VERSION": spec.environment_slice.world_version,
            "SLICE_NAME": spec.environment_slice.slice_name,
            "FIXTURE_BUNDLE_REF": spec.environment_slice.fixture_bundle_ref,
            "SLICE_PROVENANCE": spec.environment_slice.provenance,
            "INVESTIGATOR_KIND": spec.investigator.kind,
            "INVESTIGATOR_REF": spec.investigator.digest_or_profile,
        }

        # Truthful trace reference metadata for the World Gateway context.
        # Only the reference reaches the sandbox; telemetry is not compiled.
        if spec.trace_ref:
            env_vars["TRACE_REF"] = json.dumps(spec.trace_ref)

        # Resolved watchdog budget. SandboxSpec validation already guarantees
        # these fit inside the container outer timeout with reserve, so the
        # bridge watchdog always fires before Modal kills the sandbox.
        env_vars["TOOL_TIMEOUT_S"] = str(spec.tool_timeout_s)
        env_vars["TOOL_RECOVERY_TIMEOUT_S"] = str(spec.tool_recovery_timeout_s)

        # The resolved outer lifetime drives the bridge's overall run cutoff so
        # the watchdog can abort and recover even when a tool starts late in
        # the run and per-tool age alone would run past the Modal kill time.
        env_vars["SANDBOX_TIMEOUT_S"] = str(spec.resource_limits.timeout_s)

        if spec.investigator.entrypoint:
            env_vars["INVESTIGATOR_ENTRYPOINT"] = spec.investigator.entrypoint

        if spec.tested_agent:
            env_vars["TESTED_AGENT_KIND"] = spec.tested_agent.kind
            env_vars["TESTED_AGENT_REF"] = spec.tested_agent.digest_or_profile
            if spec.tested_agent.entrypoint:
                env_vars["TESTED_AGENT_ENTRYPOINT"] = spec.tested_agent.entrypoint

        # Merge additional non-secret environment variables (excluding reserved keys)
        for key, value in spec.env.items():
            if key not in RESERVED_ENV_KEYS:
                env_vars[key] = value

        # Validate required BRIDGE_TOKEN secret at the launch boundary
        bridge_tok = spec.secrets.get("BRIDGE_TOKEN")
        if bridge_tok is None or not bridge_tok.get_secret_value():
            raise ValueError("SandboxSpec must contain a non-empty BRIDGE_TOKEN secret")

        # Extract secrets strictly for Modal secret injection
        raw_secrets: dict[str, str | None] = {
            key: secret.get_secret_value()
            for key, secret in spec.secrets.items()
        }

        # Command entrypoint: bridge process inside container
        command = ["python", "-m", "app.domain.agent_runner.bridge"]

        timeout_s = (
            spec.resource_limits.timeout_s
            if spec.resource_limits.timeout_s > 0
            else self.default_timeout_s
        )

        outbound_allowlist = (
            spec.outbound_domain_allowlist
            if spec.outbound_domain_allowlist
            else self.default_outbound_domain_allowlist
        )

        sandbox_id = _modal_client.create_sandbox(
            self.app_name,
            command,
            env=env_vars,
            secrets=raw_secrets,
            timeout_s=timeout_s,
            cpus=float(spec.resource_limits.cpus),
            memory_mib=spec.resource_limits.memory_mib,
            outbound_domain_allowlist=outbound_allowlist if outbound_allowlist else None,
        )

        return SandboxHandle(
            sandbox_id=sandbox_id,
            provider="modal",
            created_at=datetime.now(timezone.utc),
        )

    def get_status(self, handle: SandboxHandle) -> RunnerStatus:
        """Check the execution status of a Modal sandbox."""
        exit_code = _modal_client.poll_sandbox(handle.sandbox_id)
        if exit_code is None:
            return RunnerStatus(
                sandbox_id=handle.sandbox_id,
                state=RunnerState.running,
            )
        if exit_code == 0:
            return RunnerStatus(
                sandbox_id=handle.sandbox_id,
                state=RunnerState.completed,
                exit_code=0,
            )
        return RunnerStatus(
            sandbox_id=handle.sandbox_id,
            state=RunnerState.failed,
            exit_code=exit_code,
        )

    def terminate(self, handle: SandboxHandle) -> None:
        """Terminate the Modal sandbox container. Safe to call multiple times."""
        _modal_client.terminate_sandbox(handle.sandbox_id)
