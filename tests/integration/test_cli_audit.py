"""This module tests the Phase 7.7 audit command against isolated PostgreSQL.

The audit scans saved bundles and generated reports for secrets and
forbidden data, proves evidence visibility, repeats one deterministic run,
verifies persistent tables stay unchanged and unlocked, and writes the
structured report. Tests run only against the configured isolated database.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from pydantic import ValidationError
from sqlalchemy import delete

from app.config import Settings
from app.db import get_session_factory
from app.domain.regression.models import RegressionCaseRecord
from app.domain.suite.models import RegressionSuiteRecord


@pytest.fixture(scope="module", autouse=True)
def apply_audit_migrations() -> None:
    try:
        Settings()  # type: ignore[call-arg]
    except ValidationError:
        pytest.skip("DATABASE_URL is required for audit integration tests")
    command.upgrade(Config("alembic.ini"), "head")


def _lab(*args: str, expect: int = 0) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        [sys.executable, "-m", "app.cli.main", *args],
        capture_output=True,
        env={**os.environ, "ENVIRONMENT": "test"},
        text=True,
        timeout=600,
    )
    assert result.returncode == expect, f"lab {' '.join(args)} failed: {result.stderr}"
    return result


async def _cleanup() -> None:
    async with get_session_factory().begin() as session:
        await session.execute(delete(RegressionSuiteRecord))
        await session.execute(delete(RegressionCaseRecord))


@pytest.mark.integration
async def test_audit_run_scans_and_proves_isolation(tmp_path: Path) -> None:
    artifacts_root = tmp_path / "artifacts"
    review_file = tmp_path / "review.json"
    review_file.write_text(
        json.dumps(
            {
                "approved_request_message": "Use the approved synthetic request.",
                "reviewer": "alice",
                "reviewed_at": "2026-08-08T00:00:00Z",
                "reason": "Reviewed and approved",
                "source_evidence": "fixture:langsmith",
            }
        ),
        encoding="utf-8",
    )
    try:
        # Seed the library so the audit scans real saved bundles.
        _lab(
            "proof",
            "eight",
            "--review-file",
            str(review_file),
            "--offline",
            "--out",
            str(artifacts_root / "proof"),
        )
        _lab("reference", "report", "--out", str(artifacts_root / "reference"))

        result = _lab("audit", "run", "--artifacts-root", str(artifacts_root))
        assert "Audit complete" in result.stdout

        report_path = artifacts_root / "audit" / "phase7-audit.json"
        report = json.loads(report_path.read_text())
        assert report["environment"] == "test"
        assert report["scanned_bundles"] == 8
        # One proof report and one reference report are created by this test.
        # Do not rely on artifacts left by an earlier local run.
        assert report["scanned_artifacts"] == 2
        assert report["findings"] == []
        facts = "\n".join(report["facts"])
        assert "persistent support tables are unchanged" in facts
        assert "no transaction is left open" in facts
        assert "identical verdict" in facts
        assert "carry source evidence and configuration versions" in facts
        # The audit must scan the 7.6 reference report, not only the proof.
        assert "reference report(s)" in facts

        markdown = (artifacts_root / "audit" / "phase7-audit.md").read_text()
        assert "## Facts" in markdown
        assert "## Risks" in markdown
    finally:
        await _cleanup()
