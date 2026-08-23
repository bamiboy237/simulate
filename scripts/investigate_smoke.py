"""Live end-to-end smoke verification for Modal investigation runner and Prime Agent.

Usage:
    SIMULATE_LIVE_E2E=1 uv run python scripts/investigate_smoke.py

Skips cleanly when SIMULATE_LIVE_E2E is unset or credentials are absent.
"""

import asyncio
import os
import sys
from uuid import uuid4

from app.config import get_settings
from app.db import get_session_factory
from app.domain.investigation.brief import render_task_brief
from app.domain.investigation.repository import SqlAlchemyInvestigationRepository
from app.domain.investigation.schemas import (
    InvestigationCreateRequest,
    InvestigationStatus,
)
from app.domain.investigation.service import InvestigationService
from app.domain.runner import ModalRunner
from app.domain.runner.schemas import (
    AgentArtifactRef,
    EnvironmentSliceRef,
    ResourceLimits,
)


async def run_live_smoke() -> int:
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
            control_plane_url=settings.control_plane_public_url or "http://127.0.0.1:8000",
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
        resp, token = await service.start(request)
        await session.commit()
        print(f"Investigation created: {resp.id} (status={resp.status.value})")

        # Monitor live event stream until terminal state or timeout
        print("Streaming live events from container...")
        max_polls = 60
        for _ in range(max_polls):
            await asyncio.sleep(2)
            detail = await service.get_by_id(resp.id)
            if detail.status in {
                InvestigationStatus.COMPLETED,
                InvestigationStatus.FAILED,
                InvestigationStatus.CANCELLED,
            }:
                print(f"Investigation reached terminal status: {detail.status.value}")
                if detail.summary:
                    print(f"Summary Findings: {detail.summary.findings}")
                    print(f"Recommended Next Step: {detail.summary.next_step}")
                break

        print("=== Smoke test run finished ===")
        return 0


def main() -> int:
    return asyncio.run(run_live_smoke())


if __name__ == "__main__":
    sys.exit(main())
