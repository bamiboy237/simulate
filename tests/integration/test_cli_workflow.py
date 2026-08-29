"""This module tests the offline lab command-line workflow end to end.

The documented sequence imports or loads a fixture, compiles a reviewed
bundle, runs it, saves the case, creates a suite, runs a suite comparison,
and produces the eight-scenario proof report. Everything runs without
hosted-model or observability credentials against the isolated database.
"""

import json
import os
import pathlib
import subprocess
import sys

import pytest
from alembic import command
from alembic.config import Config
from pydantic import ValidationError
from sqlalchemy import delete

from app.config import Settings
from app.db import get_session_factory
from app.domain.regression.models import RegressionCaseRecord
from app.domain.suite.models import RegressionSuiteRecord


@pytest.fixture()
def review_file(tmp_path: pathlib.Path) -> str:
    """This fixture writes a synthetic approved review for the offline proof."""
    path = tmp_path / "review.json"
    path.write_text(
        json.dumps(
            {
                "approved_request_message": (
                    "Use the approved synthetic request for this simulation."
                ),
                "reviewer": "alice",
                "reviewed_at": "2026-08-08T00:00:00Z",
                "reason": "Reviewed and approved",
                "source_evidence": "fixture:langsmith",
            }
        )
    )
    return str(path)


@pytest.fixture()
def candidate_file(tmp_path: pathlib.Path) -> str:
    """This fixture writes a one-variable model candidate for the offline proof."""
    path = tmp_path / "candidate.json"
    path.write_text(
        json.dumps(
            {
                "change_type": "model",
                "candidate": {
                    "model_provider": "openai",
                    "model_name": "gpt-5.3",
                    "workflow": "support_agent",
                    "workflow_version": "2.0.0",
                    "configuration_version": "2.0.0",
                    "routing_instructions_version": "1",
                    "answer_instructions_version": "1",
                },
            }
        )
    )
    return str(path)


@pytest.fixture(scope="module", autouse=True)
def apply_cli_migrations() -> None:
    try:
        Settings()  # type: ignore[call-arg]
    except ValidationError:
        pytest.skip("DATABASE_URL is required for CLI workflow integration tests")
    command.upgrade(Config("alembic.ini"), "head")


def _lab(*args: str, expect: int = 0) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        [sys.executable, "-m", "app.cli.main", *args],
        capture_output=True,
        env={**os.environ, "ENVIRONMENT": "test"},
        text=True,
        timeout=300,
    )
    assert result.returncode == expect, f"lab {' '.join(args)} failed: {result.stderr}"
    return result


async def _cleanup() -> None:
    async with get_session_factory().begin() as session:
        await session.execute(
            delete(RegressionSuiteRecord).where(
                RegressionSuiteRecord.name.in_(["cli-suite", "phase7-proof"])
            )
        )
        await session.execute(delete(RegressionCaseRecord))


@pytest.mark.integration
async def test_offline_cli_workflow_end_to_end(
    review_file: str, candidate_file: str
) -> None:
    try:
        scenario = _lab("scenario", "create", "phase2-03-database-timeout")
        assert "phase2-03-database-timeout" in scenario.stdout

        compiled = _lab(
            "bundle", "compile", "phase2-03-database-timeout", "--review-file", review_file
        )
        assert "Compiled bundle" in compiled.stdout

        ran = _lab("run", "artifacts/bundles/phase2-03-database-timeout.json", "--offline")
        assert "reproduced" in ran.stdout

        added = _lab(
            "regression",
            "add",
            "artifacts/bundles/phase2-03-database-timeout.json",
            "--source-type",
            "incident",
        )
        assert "created" in added.stdout
        case_id = added.stdout.split("case ")[1].split(" v")[0]

        cases = _lab("cases", "list")
        assert case_id in cases.stdout

        suite = _lab("suite", "create", "cli-suite", "--member", f"{case_id}@1")
        assert "created" in suite.stdout

        suite_run = _lab(
            "suite", "run", "cli-suite", "--candidate-config", candidate_file, "--offline"
        )
        assert "Suite cli-suite" in suite_run.stdout
        assert "Verdict:" in suite_run.stdout

        proof = _lab("proof", "eight", "--review-file", review_file, "--offline")
        assert "8 saved and run" in proof.stdout

        report_path = "artifacts/proof/eight-case-report.json"
        first_report = json.loads(open(report_path).read())
        assert len(first_report["scenarios"]) == 8
        assert all(item["status"] == "saved_and_run" for item in first_report["scenarios"])
        for item in first_report["scenarios"]:
            assert item["bundle_content_hash"]
            assert item["evidence_ref"] is not None
            assert item["case_id"] is not None
        # The offline candidate must differ in exactly one declared dimension
        # and show both a passing and a regressing case.
        totals = first_report["totals"]
        assert totals["candidate_passes"] >= 1, (
            "offline proof must include at least one passing candidate"
        )
        assert totals["candidate_regresses"] >= 1, (
            "offline proof must include at least one case-level regression"
        )
        per_case = {
            item["scenario_id"]: item["comparison"]["verdict"]
            for item in first_report["scenarios"]
        }
        assert per_case["phase2-05-unconfirmed-refund"] == "candidate_passes"
        assert per_case["phase2-03-database-timeout"] == "candidate_regresses"

        _lab("proof", "eight", "--review-file", review_file, "--offline")
        second_report = json.loads(open(report_path).read())

        def stable_summary(report):
            "Keep only the deterministic report fields."
            totals = report["totals"]
            return {
                "suite_id": report["suite_id"],
                "suite_version": report["suite_version"],
                "suite_name": report["suite_name"],
                "verdict": report["verdict"],
                "verdict_reason": report["verdict_reason"],
                "totals": {
                    key: totals[key]
                    for key in (
                        "cases",
                        "comparable",
                        "candidate_passes",
                        "candidate_regresses",
                        "no_material_difference",
                        "insufficient_evidence",
                        "safety_regressions",
                        "missing_measurement_cases",
                        "coverage_failure_cases",
                    )
                }
                | {
                    "baseline": {
                        key: totals["baseline"][key]
                        for key in (
                            "success_count",
                            "total_tokens",
                            "total_cost_usd",
                            "total_retries",
                        )
                    },
                    "candidate": {
                        key: totals["candidate"][key]
                        for key in (
                            "success_count",
                            "total_tokens",
                            "total_cost_usd",
                            "total_retries",
                        )
                    },
                },
                "scenarios": [
                    {
                        "scenario_id": item["scenario_id"],
                        "status": item["status"],
                        "case_id": item["case_id"],
                        "case_version": item["case_version"],
                        "bundle_content_hash": item["bundle_content_hash"],
                        "evidence_ref": item["evidence_ref"],
                        "evidence_content_hash": item["evidence_content_hash"],
                        "configuration_version": item["configuration_version"],
                        "comparison_verdict": (
                            item["comparison"]["verdict"]
                            if item.get("comparison") is not None
                            else None
                        ),
                    }
                    for item in report["scenarios"]
                ],
            }

        assert stable_summary(first_report) == stable_summary(second_report), (
            "proof report must be deterministic except run ids and wall-clock timing"
        )

        structured = _lab("--json", "cases", "list")
        assert json.loads(structured.stdout) is not None

        bad = _lab(
            "suite", "run", "cli-suite", "--candidate-config", "missing.json", "--offline", expect=1
        )
        assert "candidate_config_invalid" in bad.stderr

        bad_bundle = _lab("run", "does-not-exist.json", "--offline", expect=1)
        assert "bundle_load_failed" in bad_bundle.stderr
    finally:
        await _cleanup()
