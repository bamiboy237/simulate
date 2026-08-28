"""Live end-to-end smoke verification for Modal investigation runner and Prime Agent.

Usage:
    SIMULATE_LIVE_E2E=1 MODAL_ENABLED=true uv run python scripts/investigate_smoke.py

Skips cleanly when SIMULATE_LIVE_E2E is unset or Modal is disabled.
Returns nonzero when the run times out, fails, completes without a persisted
summary, or the Modal sandbox is not terminated. Never prints the bridge
token or any credential.
"""

import asyncio
import os
import sys
import time
from uuid import uuid4

from app.config import get_settings
from app.db import get_session_factory
from app.domain.investigation.brief import render_task_brief
from app.domain.investigation.repository import SqlAlchemyInvestigationRepository
from app.domain.investigation.schemas import (
    TERMINAL_STATUSES,
    InvestigationCreateRequest,
    InvestigationStatus,
)
from app.domain.investigation.service import InvestigationService
from app.domain.runner import ModalRunner
from app.domain.runner.schemas import (
    AgentArtifactRef,
    EnvironmentSliceRef,
    ResourceLimits,
    RunnerState,
)


async def run_live_smoke() -> int:
    # Fit the watchdog budget inside this script's 300s sandbox: 120s tool
    # timeout + 60s recovery + 60s run reserve = 240s < 300s. SandboxSpec
    # validation will reject any configuration that exceeds the outer lifetime,
    # so the bridge watchdog always fires before Modal kills the container.
    os.environ.setdefault("TOOL_TIMEOUT_S", "120")
    os.environ.setdefault("TOOL_RECOVERY_TIMEOUT_S", "60")

    settings = get_settings()

    if not os.environ.get("SIMULATE_LIVE_E2E"):
        print("SIMULATE_LIVE_E2E is not set. Skipping live investigation smoke test.")
        return 0

    if not settings.modal_enabled:
        print("MODAL_ENABLED is false. Skipping live investigation smoke test.")
        return 0

    print("=== Starting Live Modal Investigation Smoke Test ===")
    print(f"Modal App: {settings.modal_app_name}")

    session_factory = get_session_factory()
    async with session_factory() as session:
        repo = SqlAlchemyInvestigationRepository(session)
        runner = ModalRunner(app_name=settings.modal_app_name)
        service = InvestigationService(
            repo,
            runner=runner,
            control_plane_url=settings.control_plane_public_url,
        )

        trace_id = f"smoke_tr_{uuid4().hex[:8]}"
        slice_ref = EnvironmentSliceRef(
            world_id="support_world_v1",
            world_version="1.0.0",
            slice_name="checkout_smoke_slice",
            fixture_bundle_ref="bundles/support_smoke.json",
            provenance="observed",
        )
        investigator_ref = AgentArtifactRef(
            kind="prime_profile",
            digest_or_profile="prime-investigator@0.2.0",
        )

        task_brief = render_task_brief(
            trace_id=trace_id,
            slice_ref=slice_ref,
            trace_summary="Simulated checkout failure for smoke verification.",
            evaluator_expectations=["Verify idempotency key handling in refund transactions."],
        )

        request = InvestigationCreateRequest(
            trace_ref={"trace_id": trace_id},
            world_ref={"world_id": slice_ref.world_id},
            slice_ref=slice_ref,
            investigator_ref=investigator_ref,
            task_brief=task_brief,
            resource_limits=ResourceLimits(timeout_s=300, cpus=1, memory_mib=2048),
        )

        print(f"Creating investigation for trace {trace_id}...")
        try:
            resp, _ = await service.start(request)
        except Exception as exc:
            print(f"FAIL: could not create investigation: {exc}", file=sys.stderr)
            return 1
        await session.commit()
        print(f"Investigation created: {resp.id} (status={resp.status.value})")

        # Monitor until a terminal status or the sandbox timeout elapses.
        timeout_s = request.resource_limits.timeout_s
        deadline = time.monotonic() + timeout_s
        detail = None
        while time.monotonic() < deadline:
            await asyncio.sleep(2)
            detail = await service.get_by_id(resp.id)
            if detail.status in TERMINAL_STATUSES:
                break

        if detail is None or detail.status not in TERMINAL_STATUSES:
            handle = service._handles.get(resp.id)
            if handle is not None:
                await asyncio.to_thread(runner.terminate, handle)
            print(
                f"FAIL investigation={resp.id}: timed out after {timeout_s}s "
                "without reaching a terminal status",
                file=sys.stderr,
            )
            return 1

        if detail.status in {InvestigationStatus.FAILED, InvestigationStatus.CANCELLED}:
            print(
                f"FAIL investigation={resp.id}: ended status={detail.status.value} "
                f"error={detail.error}",
                file=sys.stderr,
            )
            return 1

        if detail.summary is None:
            print(
                f"FAIL investigation={resp.id}: completed without a persisted summary",
                file=sys.stderr,
            )
            return 1

        handle = service._handles.get(resp.id)
        if handle is None:
            print(
                f"FAIL investigation={resp.id}: no sandbox handle was created",
                file=sys.stderr,
            )
            return 1

        status = await asyncio.to_thread(runner.get_status, handle)
        for _ in range(15):
            if status.state != RunnerState.running:
                break
            await asyncio.sleep(1)
            status = await asyncio.to_thread(runner.get_status, handle)
        if status.state == RunnerState.running:
            print(
                f"FAIL investigation={resp.id}: sandbox still running after terminal status",
                file=sys.stderr,
            )
            return 1
        if status.state != RunnerState.completed or status.exit_code != 0:
            print(
                f"FAIL investigation={resp.id}: sandbox ended state={status.state.value} "
                f"exit_code={status.exit_code}",
                file=sys.stderr,
            )
            return 1

        print(f"PASS investigation={resp.id}: completed with summary")
        return 0


def main() -> int:
    return asyncio.run(run_live_smoke())


if __name__ == "__main__":
    sys.exit(main())
