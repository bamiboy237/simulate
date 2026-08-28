"""Manual smoke test for Modal sandbox creation, execution, and termination.

Run this script directly when Modal credentials are configured locally:
    uv run python scripts/modal_smoke.py
"""

import sys
import time
from uuid import uuid4

from pydantic import SecretStr

from app.config import get_settings
from app.domain.runner import (
    AgentArtifactRef,
    EnvironmentSliceRef,
    ModalRunner,
    ResourceLimits,
    SandboxSpec,
)


def main() -> int:
    settings = get_settings()
    print("=== Modal Sandbox Smoke Test ===")
    print(f"App name: {settings.modal_app_name}")
    print(f"Modal enabled: {settings.modal_enabled}")

    runner = ModalRunner(
        app_name=settings.modal_app_name,
        default_outbound_domain_allowlist=settings.modal_outbound_domain_allowlist,
    )
    spec = SandboxSpec(
        spec_version="1.0.0",
        investigation_id=uuid4(),
        task_brief="# Modal smoke test",
        environment_slice=EnvironmentSliceRef(
            world_id="smoke_test",
            world_version="1.0",
            slice_name="smoke_slice",
            fixture_bundle_ref="smoke_ref",
            provenance="observed",
        ),
        investigator=AgentArtifactRef(
            kind="prime_profile",
            digest_or_profile="prime@test",
            entrypoint="python -c \"print('Hello from Modal sandbox!')\"",
        ),
        env={"SMOKE_TEST": "true"},
        secrets={
            "BRIDGE_TOKEN": SecretStr("smoke-bridge-token"),
            "TEST_SECRET": SecretStr("smoke-secret-val"),
        },
        callback_base_url="https://api.simulate.local",
        resource_limits=ResourceLimits(timeout_s=120, cpus=1, memory_mib=1024),
        outbound_domain_allowlist=["api.simulate.local", "api.openai.com"],
    )

    print("Launching sandbox container...")
    try:
        handle = runner.create_sandbox(spec)
        print(f"Sandbox created successfully: {handle.sandbox_id} (provider={handle.provider})")

        print("Polling status...")
        time.sleep(2)
        status = runner.get_status(handle)
        print(f"Initial status: state={status.state} exit_code={status.exit_code}")

        print("Terminating sandbox...")
        runner.terminate(handle)
        print("Sandbox terminated successfully.")

        # Test idempotency
        runner.terminate(handle)
        print("Idempotent termination confirmed.")
        print("=== Smoke test completed successfully ===")
        return 0
    except Exception as exc:
        print(f"Smoke test failed with error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
