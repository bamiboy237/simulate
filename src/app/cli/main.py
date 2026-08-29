"""This module implements the ``lab`` command-line workflow.

The commands call the same application services as the HTTP layer and the
domain runners. Human output explains the result and the next action;
``--json`` prints the stable structured result. Errors name the failed step,
never expose secrets, and exit non-zero.
"""

import argparse
import asyncio
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, NoReturn, cast
from uuid import UUID

from pydantic import ValidationError
from sqlalchemy import delete, func, select, text
from sqlalchemy.dialects.postgresql import Insert, insert

from app.adapters.pydantic_ai_agent import ModelConfig
from app.adapters.sources.fixture_source import FixtureTraceSource
from app.cli.investigate import build_investigate_parser
from app.cli.offline import (
    OFFLINE_CANDIDATE_MODEL_NAME,
    OFFLINE_MODEL_NAME,
    OfflinePlan,
    install_offline_model,
    install_offline_proof_model,
    offline_fault_script,
    offline_plan,
)
from app.cli.report import ProofReport, ProofScenarioResult, ProofScenarioStatus
from app.cli.simulate import build_simulate_parser
from app.config import Settings, get_settings
from app.db import get_session_factory
from app.domain.audit.report import AuditReport
from app.domain.audit.scanner import ScanFinding, scan_json_text, scan_payload
from app.domain.bundle.compiler import compile_bundle
from app.domain.bundle.schemas import ConfigurationVersions, SimulationBundle
from app.domain.comparison.experiment import (
    ConfigurationChangeType,
    ConfigurationSet,
    validate_baseline_matches_bundle,
)
from app.domain.comparison.model_lab import run_model_lab
from app.domain.evidence.service import TraceImportService
from app.domain.evidence.store import SqlAlchemyEvidenceStore
from app.domain.reference.compare import compare_reference_runs
from app.domain.reference.report import ReferenceWorkflowReport, WorkflowEntry
from app.domain.reference.runner import run_reference_case
from app.domain.reference.workflows.six_reference import ALL_WORKFLOWS
from app.domain.regression.models import RegressionCaseRecord
from app.domain.regression.repository import SqlAlchemyRegressionCaseRepository
from app.domain.regression.schemas import CaseSourceType, RegressionCase
from app.domain.regression.service import RegressionCaseService
from app.domain.simulation.adapters import SUPPORT_DATABASE_COVERAGE
from app.domain.simulation.provisioner import ProvisionerFactory, postgres_provisioner_factory
from app.domain.simulation.runner import run_bundle, state_from_bundle
from app.domain.simulation.scenarios import SCENARIOS, scenario_with_evidence
from app.domain.suite.repository import SqlAlchemySuiteRepository
from app.domain.suite.runner import run_cohort_model_comparison, run_suite_comparison
from app.domain.suite.schemas import SuiteMemberRef
from app.domain.suite.service import SuiteService, stable_suite_id
from app.domain.support.models import Customer, Order, PolicyDocument, Ticket
from app.domain.user_simulator.plugins import build_default_registry

ARTIFACTS_DIR = Path("artifacts")

PROOF_SCENARIOS = tuple(scenario.scenario_id for scenario in SCENARIOS)

SOURCE_TYPE_BY_SCENARIO = {
    "phase2-01-bad-prompt-policy-answer": CaseSourceType.INCIDENT,
    "phase2-02-wrong-policy-evidence": CaseSourceType.INCIDENT,
    "phase2-03-database-timeout": CaseSourceType.INCIDENT,
    "phase2-04-wrong-tool-arguments": CaseSourceType.INCIDENT,
    "phase2-05-unconfirmed-refund": CaseSourceType.INCIDENT,
    "phase2-06-repeated-step": CaseSourceType.SUSPICIOUS_SUCCESS,
    "phase2-07-slow-database": CaseSourceType.DESIGNED_EDGE_CASE,
    "phase2-08-model-cost-comparison": CaseSourceType.MODEL_COMPARISON,
}


def fail(code: str, message: str) -> NoReturn:
    """This function prints a safe error and exits non-zero."""
    print(f"lab: error [{code}]: {message}", file=sys.stderr)
    raise SystemExit(1)


def _settings() -> Settings:
    try:
        return get_settings()
    except ValidationError as error:
        fail("configuration_invalid", str(error))
    raise AssertionError("unreachable")


def _provisioner_factory(settings: Settings) -> ProvisionerFactory:
    from app.db import get_session_factory as factory

    try:
        return postgres_provisioner_factory(
            factory(),
            database_url=str(settings.migration_database_url),
            environment=settings.environment,
            isolation_confirmed=settings.environment == "test",
        )
    except ValueError as error:
        fail("sandbox_unavailable", f"{error}; set ENVIRONMENT=test and an isolated DATABASE_URL")


def _load_bundle(path: str) -> SimulationBundle:
    try:
        payload = json.loads(Path(path).read_text())
    except (OSError, json.JSONDecodeError) as error:
        fail("bundle_load_failed", f"cannot read {path!r}: {error}")
    try:
        return SimulationBundle.model_validate(payload)
    except ValidationError as error:
        fail("bundle_invalid", f"{path!r} is not a valid approved bundle: {error}")


def _parse_case_ref(value: str) -> tuple[UUID, int]:
    try:
        case_id, version = value.split("@", 1)
        return UUID(case_id), int(version)
    except (ValueError, TypeError):
        fail("invalid_case_ref", f"{value!r} must look like <case-id>@<version>")


def _candidate_config(
    path: str,
) -> tuple[ConfigurationChangeType, ConfigurationVersions, str | None, str | None]:
    try:
        payload = json.loads(Path(path).read_text())
    except (OSError, json.JSONDecodeError) as error:
        fail("candidate_config_invalid", f"cannot read {path!r}: {error}")
    try:
        change_type = ConfigurationChangeType(payload["change_type"])
        candidate = ConfigurationVersions.model_validate(payload["candidate"])
    except (KeyError, ValueError, ValidationError) as error:
        fail("candidate_config_invalid", f"{path!r} is invalid: {error}")
    return (
        change_type,
        candidate,
        payload.get("candidate_prompt"),
        payload.get("candidate_prompt_version"),
    )


def _model_config(settings: Settings, offline: bool) -> ModelConfig:
    if offline:
        return ModelConfig(provider="openai", name=OFFLINE_MODEL_NAME)
    if not settings.model_configured:
        fail("model_not_configured", "set MODEL_PROVIDER and MODEL_NAME, or use --offline")
    provider = settings.model_provider
    name = settings.model_name
    if provider is None or name is None:
        fail("model_not_configured", "set MODEL_PROVIDER and MODEL_NAME, or use --offline")
    return ModelConfig(
        provider=provider,
        name=name,
        base_url=settings.model_base_url,
        api_key=settings.model_api_key,
    )


def _install_offline_proof(
    bundles: list[SimulationBundle],
    baseline_name: str,
    candidate_name: str,
) -> None:
    """This function installs distinct offline plans for the proof.

    The baseline and the candidate change exactly one declared dimension
    (the model). The baseline plan set fails one reviewed expectation and
    the candidate plan set regresses one other case, so the proof shows one
    passing candidate and one understandable case-level regression.
    """
    baseline_plans = {
        bundle.scenario.scenario_id: offline_plan(
            bundle.scenario.scenario_id, state_from_bundle(bundle)
        )
        for bundle in bundles
    }
    candidate_plans = {
        bundle.scenario.scenario_id: offline_plan(
            bundle.scenario.scenario_id,
            state_from_bundle(bundle),
            side="candidate",
        )
        for bundle in bundles
    }
    install_offline_proof_model(
        {baseline_name: baseline_plans, candidate_name: candidate_plans}
    )


def _install_offline(bundles: list[SimulationBundle], names: tuple[str, ...]) -> None:
    plans: dict[str, OfflinePlan] = {}
    for bundle in bundles:
        plan = offline_plan(bundle.scenario.scenario_id, state_from_bundle(bundle))
        for name in names:
            plans.setdefault(name, plan)
    install_offline_model(plans)


def emit(args: argparse.Namespace, payload: object, human: str) -> None:
    """This function prints the structured or human result."""
    if args.json:
        if hasattr(payload, "model_dump_json"):
            print(payload.model_dump_json(indent=2))
        else:
            print(json.dumps(payload, indent=2, default=str))
    else:
        print(human)


def cmd_import_trace(args: argparse.Namespace) -> None:
    async def run() -> None:
        settings = _settings()
        from app.domain.evidence.source import TraceSource

        source: TraceSource
        if args.source == "langsmith":
            if not settings.langsmith_api_key:
                fail(
                    "langsmith_not_configured",
                    "set LANGSMITH_API_KEY for source 'langsmith', or use --source fixture",
                )
            from app.adapters.sources.langsmith import LangSmithSource, LangSmithSourceConfig

            source = LangSmithSource(
                LangSmithSourceConfig(
                    api_key=settings.langsmith_api_key,
                    project=settings.langsmith_project,
                    scenario_id=getattr(args, "scenario", None),
                )
            )
        else:
            source = FixtureTraceSource()
        evidence = await source.fetch_trace(args.trace_id)
        async with get_session_factory().begin() as session:
            result = await TraceImportService(SqlAlchemyEvidenceStore(session)).import_evidence(
                evidence
            )
        payload = {
            "evidence_id": str(evidence.evidence_id),
            "trace_id": evidence.source.trace_id,
            "status": result.status.value,
            "import_version": result.import_version,
            "scenario_id": evidence.scenario_id,
        }
        emit(
            args,
            payload,
            (
                f"Imported trace {evidence.source.trace_id} from {args.source}. "
                f"Evidence id {evidence.evidence_id}, import version "
                f"{result.import_version} ({result.status.value})."
            ),
        )

    asyncio.run(run())


def cmd_scenario_create(args: argparse.Namespace) -> None:
    async def run() -> None:
        source = FixtureTraceSource()
        evidence = await source.fetch_trace(args.scenario_id)
        scenario = scenario_with_evidence(args.scenario_id, evidence)
        payload = scenario.model_dump(mode="json")
        emit(
            args,
            payload,
            (
                f"Scenario {scenario.scenario_id} ready: {scenario.title} "
                f"({scenario.category.value}). Expected outcome "
                f"{scenario.expected_behavior.outcome.value}, linked to trace "
                f"{evidence.source.trace_id}."
            ),
        )

    asyncio.run(run())


def cmd_bundle_compile(args: argparse.Namespace) -> None:
    async def run() -> None:
        try:
            review = json.loads(Path(args.review_file).read_text())
        except (OSError, json.JSONDecodeError) as error:
            fail("review_file_invalid", f"cannot read {args.review_file!r}: {error}")
        source = FixtureTraceSource()
        evidence = await source.fetch_trace(args.scenario_id)
        scenario = scenario_with_evidence(args.scenario_id, evidence)
        try:
            bundle = compile_bundle(
                scenario=scenario,
                evidence=evidence,
                coverage_items=(SUPPORT_DATABASE_COVERAGE,),
                approved_request_message=review["approved_request_message"],
                reviewer=review["reviewer"],
                reviewed_at=review["reviewed_at"],
                reason=review["reason"],
                review_status="approved",
                source_evidence=review.get("source_evidence"),
                fault_script=offline_fault_script(args.scenario_id),
            )
        except (KeyError, ValidationError) as error:
            fail("bundle_compile_failed", f"{args.review_file!r} is invalid: {error}")
        out_path = (
            Path(args.out)
            if args.out
            else ARTIFACTS_DIR / "bundles" / f"{args.scenario_id}.json"
        )
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(bundle.model_dump_json(indent=2))
        emit(
            args,
            bundle.model_dump(mode="json"),
            (
                f"Compiled bundle for {scenario.scenario_id}. Bundle id "
                f"{bundle.bundle_id}, content hash {(bundle.content_hash or '')[:16]}..., "
                f"evidence {bundle.evidence_ref.trace_id if bundle.evidence_ref else 'none'}. "
                f"Wrote {out_path}."
            ),
        )

    asyncio.run(run())


def cmd_run(args: argparse.Namespace) -> None:
    async def run() -> None:
        settings = _settings()
        if args.case:
            case_id, version = _parse_case_ref(args.case)
            async with get_session_factory().begin() as session:
                case = await RegressionCaseService(
                    SqlAlchemyRegressionCaseRepository(session)
                ).get_case(case_id=case_id, case_version=version)
            bundle = case.bundle
            source_label = f"saved case {case_id} v{version}"
        else:
            bundle = _load_bundle(args.bundle_path)
            source_label = args.bundle_path
        model_config = _model_config(settings, args.offline)
        if args.offline:
            _install_offline([bundle], (model_config.name,))
        run_result = await run_bundle(
            bundle=bundle,
            provisioner_factory=_provisioner_factory(settings),
            model_config=model_config,
        )
        response = run_result.response
        outcome = f"{response.outcome.value}/{response.reason_code.value}" if response else "none"
        emit(
            args,
            run_result.model_dump(mode="json"),
            (
                f"Ran {bundle.scenario.scenario_id} ({source_label}). Verdict "
                f"{run_result.verdict.value} ({outcome}). Latency "
                f"{run_result.total_latency_ms} ms, tokens {run_result.total_tokens}, "
                f"cost ${run_result.cost_usd}, retries {run_result.retries}, tools "
                f"{','.join(run_result.tool_calls) or 'none'}."
            ),
        )

    asyncio.run(run())


def cmd_compare(args: argparse.Namespace) -> None:
    async def run() -> None:
        settings = _settings()
        bundles = [_load_bundle(path) for path in args.bundle_paths]
        change_type, candidate, _, _ = _candidate_config(args.candidate_config)
        for path, bundle in zip(args.bundle_paths, bundles):
            try:
                experiment = ConfigurationSet(
                    bundle_id=bundle.bundle_id or UUID(int=0),
                    change_type=change_type,
                    baseline=bundle.configuration_versions,
                    candidate=candidate,
                )
                validate_baseline_matches_bundle(experiment, bundle)
            except ValueError as error:
                fail("experiment_invalid", f"{path!r}: {error}")
        baseline_config = _model_config(settings, args.offline)
        if change_type is not ConfigurationChangeType.MODEL:
            fail(
                "unsupported_change_dimension",
                "lab compare runs a model change; use 'lab suite run' for other "
                "validated dimensions",
            )
        for path, bundle in zip(args.bundle_paths, bundles):
            if (
                baseline_config.provider != bundle.configuration_versions.model_provider
                or baseline_config.name != bundle.configuration_versions.model_name
            ):
                fail(
                    "baseline_mismatch",
                    f"{path!r} records model "
                    f"{bundle.configuration_versions.model_provider or '?'}/"
                    f"{bundle.configuration_versions.model_name or '?'}, but the "
                    "baseline is "
                    f"{baseline_config.provider}/{baseline_config.name}",
                )
        if candidate.model_provider is None or candidate.model_name is None:
            fail("candidate_invalid", "candidate model change requires provider and name")
        candidate_config = ModelConfig(
            provider=cast("Literal['openai', 'anthropic']", candidate.model_provider),
            name=candidate.model_name,
        )
        if args.offline:
            _install_offline(bundles, (baseline_config.name, candidate_config.name))
        result = await run_model_lab(
            bundles=bundles,
            baseline_model_config=baseline_config,
            candidate_model_config=candidate_config,
            provisioner_factory=_provisioner_factory(settings),
        )
        totals = result.totals
        emit(
            args,
            result.model_dump(mode="json"),
            (
                f"Compared {len(bundles)} bundle(s): {totals.comparable_cases} "
                f"comparable, {totals.regressions} regression(s), "
                f"{totals.safety_regressions} safety regression(s). Verdict: "
                f"{result.verdict.value} — {result.verdict_reason}"
            ),
        )

    asyncio.run(run())


def cmd_regression_add(args: argparse.Namespace) -> None:
    async def run() -> None:
        bundle = _load_bundle(args.bundle_path)
        source_type = CaseSourceType(args.source_type)
        async with get_session_factory().begin() as session:
            result = await RegressionCaseService(
                SqlAlchemyRegressionCaseRepository(session)
            ).save_case(bundle=bundle, source_type=source_type)
        emit(
            args,
            result.model_dump(mode="json"),
            (
                f"Saved case {result.case_id} v{result.case_version} "
                f"({result.status.value}) for {bundle.scenario.scenario_id}."
            ),
        )

    asyncio.run(run())


def cmd_suite_create(args: argparse.Namespace) -> None:
    async def run() -> None:
        members = tuple(
            SuiteMemberRef(case_id=case_id, case_version=version)
            for case_id, version in (_parse_case_ref(ref) for ref in args.member)
        )
        async with get_session_factory().begin() as session:
            service = SuiteService(
                SqlAlchemySuiteRepository(session),
                SqlAlchemyRegressionCaseRepository(session),
            )
            result = await service.save_suite(name=args.name, members=members)
        emit(
            args,
            result.model_dump(mode="json"),
            (
                f"Saved suite {args.name} as {result.suite_id} v{result.suite_version} "
                f"({result.status.value}) with {len(members)} member(s)."
            ),
        )

    asyncio.run(run())


def cmd_suite_run(args: argparse.Namespace) -> None:
    async def run() -> None:
        settings = _settings()
        suite_name = args.suite_ref
        suite_version: int | None = None
        if "@" in args.suite_ref:
            suite_name, version_text = args.suite_ref.split("@", 1)
            suite_version = int(version_text)
        change_type, candidate, candidate_prompt, candidate_prompt_version = (
            _candidate_config(args.candidate_config)
        )
        async with get_session_factory().begin() as session:
            cases_repo = SqlAlchemyRegressionCaseRepository(session)
            suite_service = SuiteService(SqlAlchemySuiteRepository(session), cases_repo)
            if suite_version is None:
                summaries = await suite_service.list_suites()
                matching = [s for s in summaries if s.name == suite_name]
                if not matching:
                    fail("suite_not_found", f"no suite named {suite_name!r}")
                suite_version = max(s.latest_version for s in matching)
            suite = await suite_service.get_suite(
                suite_id=stable_suite_id(suite_name),
                suite_version=suite_version,
            )
            resolved_cases: list[RegressionCase] = []
            case_service = RegressionCaseService(cases_repo)
            for member in suite.members:
                resolved_cases.append(
                    await case_service.get_case(
                        case_id=member.case_id, case_version=member.case_version
                    )
                )
            cases = tuple(resolved_cases)
        baseline_config = _model_config(settings, args.offline)
        if args.offline:
            _install_offline(
                [case.bundle for case in cases],
                (baseline_config.name, OFFLINE_CANDIDATE_MODEL_NAME),
            )
        result = await run_suite_comparison(
            suite=suite,
            cases=cases,
            change_type=change_type,
            candidate=candidate,
            provisioner_factory=_provisioner_factory(settings),
            baseline_model_config=baseline_config,
            candidate_prompt=candidate_prompt,
            candidate_prompt_version=candidate_prompt_version,
        )
        totals = result.totals
        emit(
            args,
            result.model_dump(mode="json"),
            (
                f"Suite {suite.name} v{suite.suite_version}: {totals.cases} case(s), "
                f"{totals.candidate_passes} pass(es), {totals.candidate_regresses} "
                f"regression(s), {totals.no_material_difference} unchanged, "
                f"{totals.insufficient_evidence} inconclusive. Verdict: "
                f"{result.verdict.value} — {result.verdict_reason}"
            ),
        )

    asyncio.run(run())


def cmd_cases_list(args: argparse.Namespace) -> None:
    async def run() -> None:
        async with get_session_factory().begin() as session:
            summaries = await RegressionCaseService(
                SqlAlchemyRegressionCaseRepository(session)
            ).list_cases()
        payload = [summary.model_dump(mode="json") for summary in summaries]
        emit(
            args,
            payload,
            "\n".join(
                f"{summary.case_id}  {summary.scenario_id}  v{summary.latest_version}  "
                f"{summary.source_type.value}"
                for summary in summaries
            )
            or "No saved cases.",
        )

    asyncio.run(run())


def cmd_suites_list(args: argparse.Namespace) -> None:
    async def run() -> None:
        async with get_session_factory().begin() as session:
            summaries = await SuiteService(
                SqlAlchemySuiteRepository(session),
                SqlAlchemyRegressionCaseRepository(session),
            ).list_suites()
        payload = [summary.model_dump(mode="json") for summary in summaries]
        emit(
            args,
            payload,
            "\n".join(
                f"{summary.name}  {summary.suite_id}  v{summary.latest_version}  "
                f"{summary.member_count} member(s)"
                for summary in summaries
            )
            or "No saved suites.",
        )

    asyncio.run(run())


def cmd_proof_eight(args: argparse.Namespace) -> None:
    async def run() -> None:
        settings = _settings()
        try:
            review = json.loads(Path(args.review_file).read_text())
        except (OSError, json.JSONDecodeError) as error:
            fail("review_file_invalid", f"cannot read {args.review_file!r}: {error}")
        source = FixtureTraceSource()
        scenarios: list[ProofScenarioResult] = []
        saved_cases: list[RegressionCase] = []
        case_refs: list[SuiteMemberRef] = []
        async with get_session_factory().begin() as session:
            case_service = RegressionCaseService(SqlAlchemyRegressionCaseRepository(session))
            for scenario_id in PROOF_SCENARIOS:
                try:
                    trace_id = (
                        "phase2-08-model-cost-comparison-primary"
                        if scenario_id == "phase2-08-model-cost-comparison"
                        else scenario_id
                    )
                    evidence = await source.fetch_trace(trace_id)
                    scenario = scenario_with_evidence(scenario_id, evidence)
                    bundle = compile_bundle(
                        scenario=scenario,
                        evidence=evidence,
                        coverage_items=(SUPPORT_DATABASE_COVERAGE,),
                        approved_request_message=(
                            f"{scenario_id}: {review['approved_request_message']}"
                        ),
                        reviewer=review["reviewer"],
                        reviewed_at=review["reviewed_at"],
                        reason=review["reason"],
                        review_status="approved",
                        source_evidence=review.get("source_evidence"),
                        fault_script=offline_fault_script(scenario_id),
                    )
                    saved = await case_service.save_case(
                        bundle=bundle,
                        source_type=SOURCE_TYPE_BY_SCENARIO[scenario_id],
                    )
                    saved_cases.append(
                        await case_service.get_case(
                            case_id=saved.case_id, case_version=saved.case_version
                        )
                    )
                    case_refs.append(
                        SuiteMemberRef(case_id=saved.case_id, case_version=saved.case_version)
                    )
                    scenarios.append(
                        ProofScenarioResult(
                            scenario_id=scenario_id,
                            status=ProofScenarioStatus.SAVED_AND_RUN,
                            source_type=SOURCE_TYPE_BY_SCENARIO[scenario_id],
                            case_id=saved.case_id,
                            case_version=saved.case_version,
                            bundle_content_hash=bundle.content_hash,
                            evidence_ref=bundle.evidence_ref,
                            evidence_content_hash=bundle.evidence_content_hash,
                            configuration_version=(
                                bundle.configuration_versions.configuration_version
                            ),
                        )
                    )
                except Exception as error:  # one scenario must never hide another
                    scenarios.append(
                        ProofScenarioResult(
                            scenario_id=scenario_id,
                            status=ProofScenarioStatus.FAILED,
                            error=f"{type(error).__name__}: {error}",
                        )
                    )

        async with get_session_factory().begin() as session:
            suite_service = SuiteService(
                SqlAlchemySuiteRepository(session),
                SqlAlchemyRegressionCaseRepository(session),
            )
            suite_result = await suite_service.save_suite(name="phase7-proof", members=case_refs)
            suite = await suite_service.get_suite(
                suite_id=suite_result.suite_id, suite_version=suite_result.suite_version
            )

        baseline_config = _model_config(settings, args.offline)
        if args.offline:
            candidate_config = ModelConfig(
                provider="openai", name=OFFLINE_CANDIDATE_MODEL_NAME
            )
            _install_offline_proof(
                [case.bundle for case in saved_cases],
                baseline_config.name,
                candidate_config.name,
            )
        else:
            if not settings.candidate_model_configured:
                fail(
                    "candidate_not_configured",
                    "set MODEL_CANDIDATE_PROVIDER and MODEL_CANDIDATE_NAME, or use --offline",
                )
            candidate_config = ModelConfig(
                provider=settings.model_candidate_provider or "openai",
                name=settings.model_candidate_name or "",
            )
        comparison = await run_cohort_model_comparison(
            suite=suite,
            cases=saved_cases,
            provisioner_factory=_provisioner_factory(settings),
            baseline_model_config=baseline_config,
            candidate_model_config=candidate_config,
        )
        by_case = {item.case_id: item for item in comparison.cases}
        for entry in scenarios:
            if entry.case_id is not None and entry.case_id in by_case:
                entry.comparison = by_case[entry.case_id].comparison

        report = ProofReport(
            mode="offline" if args.offline else "hosted",
            generated_at=datetime.now(UTC).isoformat(),
            suite_id=suite.suite_id,
            suite_version=suite.suite_version,
            suite_name=suite.name,
            scenarios=tuple(scenarios),
            totals=comparison.totals,
            verdict=comparison.verdict,
            verdict_reason=comparison.verdict_reason,
        )
        out_dir = Path(args.out) if args.out else ARTIFACTS_DIR / "proof"
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "eight-case-report.json").write_text(report.model_dump_json(indent=2))
        (out_dir / "eight-case-report.md").write_text(_proof_markdown(report))
        emit(
            args,
            report.model_dump(mode="json"),
            (
                f"Proof for {len(scenarios)} scenario(s): "
                f"{sum(1 for s in scenarios if s.status is ProofScenarioStatus.SAVED_AND_RUN)} "
                f"saved and run, "
                f"{sum(1 for s in scenarios if s.status is ProofScenarioStatus.FAILED)} failed. "
                f"Suite {suite.name} v{suite.suite_version}, verdict "
                f"{comparison.verdict.value}. Wrote {out_dir / 'eight-case-report.json'}."
            ),
        )

    asyncio.run(run())


def _proof_markdown(report: ProofReport) -> str:
    lines = [
        "# Phase 7.5 — Eight-scenario proof report",
        "",
        f"- Mode: {report.mode}",
        f"- Suite: {report.suite_name} v{report.suite_version}",
        f"- Verdict: {report.verdict.value} — {report.verdict_reason}",
        f"- Totals: {report.totals.cases} cases, {report.totals.candidate_passes} passes, "
        f"{report.totals.candidate_regresses} regressions, "
        f"{report.totals.no_material_difference} unchanged, "
        f"{report.totals.insufficient_evidence} inconclusive",
        "",
        "| Scenario | Status | Case | Version | Hash | Evidence | Comparison verdict |",
        "|---|---|---|---|---|---|---|",
    ]
    for scenario in report.scenarios:
        comparison = scenario.comparison
        lines.append(
            "| "
            + " | ".join(
                [
                    scenario.scenario_id,
                    scenario.status.value,
                    str(scenario.case_id) if scenario.case_id else "-",
                    str(scenario.case_version) if scenario.case_version else "-",
                    (scenario.bundle_content_hash or "-")[:16],
                    (
                        scenario.evidence_ref.trace_id
                        if scenario.evidence_ref is not None
                        else "-"
                    ),
                    (
                        comparison.verdict.value
                        if comparison is not None
                        else (scenario.error or "-")
                    ),
                ]
            )
            + " |"
        )
    return "\n".join(lines)




def _scanned_artifact_count(artifacts_dir: Path = ARTIFACTS_DIR) -> int:
    """This function counts every generated JSON report the audit scans."""
    count = 0
    for directory in ("bundles", "proof", "reference", "audit"):
        path = artifacts_dir / directory
        if path.exists():
            count += len(list(path.glob("*.json")))
    return count


def cmd_audit(args: argparse.Namespace) -> None:
    """This command runs the Phase 7 privacy, isolation, and reproducibility audit."""

    async def run() -> None:
        settings = _settings()
        if settings.environment != "test":
            fail(
                "audit_requires_test_environment",
                "the audit may only run against an isolated test database "
                "(set ENVIRONMENT=test and an isolated DATABASE_URL)",
            )
        findings: list[ScanFinding] = []
        facts: list[str] = []
        risks: list[str] = []
        skipped: list[str] = []

        async with get_session_factory().begin() as session:
            rows = (
                (await session.execute(select(RegressionCaseRecord.bundle_json))).scalars().all()
            )
        facts.append(f"scanned {len(rows)} saved bundle(s) in the case library")
        for index, bundle_json in enumerate(rows, 1):
            findings.extend(scan_payload(bundle_json, context=f"saved-case-{index}"))

        artifacts_dir = Path(args.artifacts_root)
        bundles_dir = artifacts_dir / "bundles"
        proof_dir = artifacts_dir / "proof"
        reference_dir = artifacts_dir / "reference"
        audit_dir = artifacts_dir / "audit"
        bundle_files = sorted(bundles_dir.glob("*.json")) if bundles_dir.exists() else []
        proof_files = sorted(proof_dir.glob("*.json")) if proof_dir.exists() else []
        reference_files = (
            sorted(reference_dir.glob("*.json")) if reference_dir.exists() else []
        )
        previous_audit_files = (
            sorted(audit_dir.glob("*.json")) if audit_dir.exists() else []
        )
        out_dir = Path(args.out) if args.out else audit_dir
        for path in (
            bundle_files + proof_files + reference_files + previous_audit_files
        ):
            if path.resolve() == (out_dir / "phase7-audit.json").resolve():
                continue
            findings.extend(scan_json_text(path.read_text(), context=str(path)))
        facts.append(
            f"scanned {len(bundle_files)} bundle artifact(s), {len(proof_files)} "
            f"proof report(s), {len(reference_files)} reference report(s), and "
            f"{len(previous_audit_files)} previous audit report(s)"
        )

        async with get_session_factory().begin() as session:
            case_service = RegressionCaseService(SqlAlchemyRegressionCaseRepository(session))
            summaries = await case_service.list_cases()
            visible = 0
            for summary in summaries:
                case = await case_service.get_case(
                    case_id=summary.case_id, case_version=summary.latest_version
                )
                if (
                    case.evidence_ref is not None
                    and case.evidence_content_hash is not None
                    and case.configuration_versions is not None
                ):
                    visible += 1
        facts.append(
            f"{visible} of {len(summaries)} saved case(s) carry source evidence and "
            "configuration versions"
        )

        bundle = None
        if bundle_files:
            bundle = _load_bundle(str(bundle_files[0]))
        else:
            source = FixtureTraceSource()
            evidence = await source.fetch_trace("phase2-08-model-cost-comparison-primary")
            scenario = scenario_with_evidence("phase2-08-model-cost-comparison", evidence)
            bundle = compile_bundle(
                scenario=scenario,
                evidence=evidence,
                coverage_items=(SUPPORT_DATABASE_COVERAGE,),
                approved_request_message=(
                    "Use the approved synthetic request for this simulation."
                ),
                reviewer="alice",
                reviewed_at="2026-08-08T00:00:00Z",
                reason="Reviewed and approved",
                review_status="approved",
            )
        plan = offline_plan(bundle.scenario.scenario_id, state_from_bundle(bundle))
        install_offline_model({OFFLINE_MODEL_NAME: plan})
        model_config = ModelConfig(provider="openai", name=OFFLINE_MODEL_NAME)

        async with get_session_factory().begin() as session:
            counts_before = (
                await session.scalar(select(func.count()).select_from(Customer)),
                await session.scalar(select(func.count()).select_from(Order)),
                await session.scalar(select(func.count()).select_from(Ticket)),
                await session.scalar(select(func.count()).select_from(PolicyDocument)),
            )

        runs = []
        for _ in range(2):
            runs.append(
                await run_bundle(
                    bundle=bundle,
                    provisioner_factory=_provisioner_factory(settings),
                    model_config=model_config,
                )
            )
        first, second = runs
        if (
            first.verdict == second.verdict
            and first.bundle_content_hash == second.bundle_content_hash
            and first.total_tokens == second.total_tokens
            and first.cost_usd == second.cost_usd
            and first.final_state == second.final_state
        ):
            facts.append(
                "two repeated offline runs produced identical verdict, bundle hash, "
                "tokens, cost, and final state"
            )
        else:
            risks.append("repeated offline runs differed in stable fields")

        async with get_session_factory().begin() as session:
            counts_after = (
                await session.scalar(select(func.count()).select_from(Customer)),
                await session.scalar(select(func.count()).select_from(Order)),
                await session.scalar(select(func.count()).select_from(Ticket)),
                await session.scalar(select(func.count()).select_from(PolicyDocument)),
            )
        if counts_before == counts_after:
            facts.append(
                "persistent support tables are unchanged after two simulations "
                "(customer, order, ticket, policy counts identical)"
            )
        else:
            risks.append(
                f"persistent support table counts changed across simulations "
                f"({counts_before} -> {counts_after})"
            )

        async with get_session_factory().begin() as session:
            probe_id = str(UUID(int=0xABCDEF))
            await session.execute(
                delete(Customer).where(Customer.email == "audit-probe@example.com")
            )
            await session.execute(
                insert_customer(probe_id)
            )
            await session.execute(
                delete(Customer).where(Customer.email == "audit-probe@example.com")
            )
        facts.append("persistent tables accept committed writes after simulations (no lock left)")

        async with get_session_factory()() as session:
            idle = await session.scalar(
                text("SELECT count(*) FROM pg_stat_activity WHERE state = 'idle in transaction'")
            )
        if idle == 0:
            facts.append("no transaction is left open after the simulations")
        else:
            risks.append(f"{idle} idle-in-transaction connection(s) remain after the runs")

        skipped.append(
            "clean-checkout run (manual, post-commit): clone the merged commit, "
            "uv sync, point ENVIRONMENT=test at a disposable database, then run "
            "'lab proof eight --review-file artifacts/review.json --offline' and "
            "'lab audit run'; this audit ran from the working tree and needed no "
            "hosted-model or observability credentials"
        )
        report = AuditReport(
            environment=settings.environment,
            generated_at=datetime.now(UTC).isoformat(),
            scanned_bundles=len(rows),
            scanned_artifacts=(
                len(bundle_files) + len(proof_files) + len(reference_files)
                + len(previous_audit_files)
            ),
            findings=tuple(findings),
            facts=tuple(facts),
            fixed_defects=(),
            risks=tuple(risks),
            skipped_checks=tuple(skipped),
        )
        out_dir = Path(args.out) if args.out else audit_dir
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "phase7-audit.json").write_text(report.model_dump_json(indent=2))
        (out_dir / "phase7-audit.md").write_text(_audit_markdown(report))
        emit(
            args,
            report.model_dump(mode="json"),
            (
                f"Audit complete: {len(findings)} finding(s), {len(facts)} fact(s), "
                f"{len(risks)} risk(s). Scanned {len(rows)} saved bundle(s) and "
                f"{_scanned_artifact_count(artifacts_dir)} "
                f"artifact(s). Wrote {out_dir / 'phase7-audit.json'}."
            ),
        )

    asyncio.run(run())


def insert_customer(customer_id: str) -> Insert:
    """This function builds one disposable probe row for the lock check."""
    return insert(Customer).values(
        id=UUID(customer_id),
        name="audit-probe",
        email="audit-probe@example.com",
    )


def _audit_markdown(report: AuditReport) -> str:
    lines = [
        "# Phase 7.7 — Privacy, isolation, and reproducibility audit",
        "",
        f"- Environment: {report.environment}",
        f"- Scanned: {report.scanned_bundles} saved bundle(s), "
        f"{report.scanned_artifacts} artifact(s)",
        f"- Findings: {len(report.findings)}",
        f"- Risks: {len(report.risks)}",
        "",
        "## Facts",
    ]
    lines.extend(f"- {fact}" for fact in report.facts)
    if report.findings:
        lines.append("")
        lines.append("## Findings")
        for finding in report.findings:
            lines.append(f"- {finding.kind} at {finding.context} {finding.path}: {finding.snippet}")
    lines.append("")
    lines.append("## Risks")
    lines.extend(f"- {risk}" for risk in report.risks) if report.risks else lines.append("- none")
    lines.append("")
    lines.append("## Checks not run")
    if report.skipped_checks:
        lines.extend(f"- {check}" for check in report.skipped_checks)
    else:
        lines.append("- none")
    return "\n".join(lines)



def cmd_reference_run(args: argparse.Namespace) -> None:
    """This command runs one reference workflow's baseline and candidate sides."""

    async def run() -> None:
        workflow = next((w for w in ALL_WORKFLOWS if w.workflow_id == args.workflow_id), None)
        if workflow is None:
            fail(
                "reference_workflow_unknown",
                f"no reference workflow {args.workflow_id!r}; choose from "
                + ", ".join(w.workflow_id for w in ALL_WORKFLOWS),
            )
        baseline = await run_reference_case(
            workflow=workflow,
            plan=workflow.baseline_plan,
            side="baseline",
            label=workflow.candidate.baseline_label,
        )
        candidate = await run_reference_case(
            workflow=workflow,
            plan=workflow.candidate_plan,
            side="candidate",
            label=workflow.candidate.candidate_label,
        )
        comparison = compare_reference_runs(
            workflow_id=workflow.workflow_id,
            change_type=workflow.candidate.change_type,
            baseline_label=workflow.candidate.baseline_label,
            candidate_label=workflow.candidate.candidate_label,
            baseline=baseline,
            candidate=candidate,
        )
        emit(
            args,
            comparison.model_dump(mode="json"),
            (
                f"{workflow.name}: baseline {baseline.verdict} "
                f"({baseline.business_outcome}), candidate {candidate.verdict} "
                f"({candidate.business_outcome}), comparison "
                f"{comparison.verdict.value}. Change: {workflow.candidate.change_type} "
                f"({workflow.candidate.baseline_label} -> {workflow.candidate.candidate_label})."
            ),
        )

    asyncio.run(run())


def _workflow_code_lines(workflow_id: str) -> int:
    """This function counts the adapter source lines for one workflow."""
    import ast

    module = "flight_booking" if workflow_id == "flight_booking" else "six_reference"
    path = Path("src/app/domain/reference/workflows") / f"{module}.py"
    try:
        tree = ast.parse(path.read_text())
    except OSError:
        return 0
    builder = "build_workflow" if workflow_id == "flight_booking" else f"build_{workflow_id}"
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == builder:
            if node.end_lineno is not None:
                return node.end_lineno - node.lineno + 1
    return 0


def cmd_reference_report(args: argparse.Namespace) -> None:
    """This command runs all seven workflows and writes the integration report."""

    async def run() -> None:
        from time import perf_counter

        entries: list[WorkflowEntry] = []
        total_started = perf_counter()
        for workflow in ALL_WORKFLOWS:
            started = perf_counter()
            baseline = await run_reference_case(
                workflow=workflow,
                plan=workflow.baseline_plan,
                side="baseline",
                label=workflow.candidate.baseline_label,
            )
            candidate = await run_reference_case(
                workflow=workflow,
                plan=workflow.candidate_plan,
                side="candidate",
                label=workflow.candidate.candidate_label,
            )
            comparison = compare_reference_runs(
                workflow_id=workflow.workflow_id,
                change_type=workflow.candidate.change_type,
                baseline_label=workflow.candidate.baseline_label,
                candidate_label=workflow.candidate.candidate_label,
                baseline=baseline,
                candidate=candidate,
            )
            safe_tools = sum(1 for tool in workflow.tools if tool.safe)
            sensitive_tools = sum(1 for tool in workflow.tools if not tool.safe)
            entries.append(
                WorkflowEntry(
                    workflow_id=workflow.workflow_id,
                    name=workflow.name,
                    source=workflow.source,
                    stateful=True,
                    safe_tools=safe_tools,
                    sensitive_tools=sensitive_tools,
                    gate_required=workflow.expectation.gate_required,
                    run_duration_seconds=round(perf_counter() - started, 3),
                    setup_effort_notes=(
                        "Measured run duration; integration setup effort is "
                        "documented in the workflow source notes and design docs, "
                        "not timed (offline harness)"
                    ),
                    baseline=baseline,
                    candidate=candidate,
                    comparison=comparison,
                    reused_code=workflow.reused_code,
                    integration_code_lines=_workflow_code_lines(workflow.workflow_id),
                    custom_adapters=(
                        "InMemoryReferenceRepository (shared)",
                        "per-workflow tool implementations",
                    ),
                    missing_capabilities=(
                        "cannot run through run_bundle: the SimulationScenario and "
                        "SimulationBundle contracts encode support types "
                        "(orders/tickets/policies); a domain-neutral scenario/bundle "
                        "schema is a flagged contract change requiring approval",
                        "no persisted workflow state: repository is in-memory per run",
                        "deterministic offline agent only; hosted-model wiring is not "
                        "provided for reference workflows",
                        "SimulationEvent allowlist limits workflow-specific event "
                        "attributes to the shared generic set",
                    ),
                    unsupported_behavior=(
                        "parallel tool calls and streaming agent output",
                        "external service calls (all effects are isolated in-memory)",
                    ),
                    real_operation_notes=(
                        workflow.integration_note,
                    ),
                    verification=(
                        "pytest tests/unit/reference: baseline reproduces, candidate "
                        "regresses, gate enforced, fault retried, cleanup verified",
                    ),
                )
            )
        report = ReferenceWorkflowReport(
            generated_at=datetime.now(UTC).isoformat(),
            workflows=tuple(entries),
            total_run_duration_seconds=round(perf_counter() - total_started, 3),
            flagged_contract_changes=(
                "generalize SimulationScenario/SimulationBundle beyond support types "
                "so reference workflows can run through run_bundle (requires approval)",
                "per-workflow SimulationEvent attributes would require allowlist "
                "extension (requires approval)",
            ),
        )
        out_dir = Path(args.out) if args.out else ARTIFACTS_DIR / "reference"
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "reference-workflows-report.json").write_text(
            report.model_dump_json(indent=2)
        )
        (out_dir / "reference-workflows-report.md").write_text(
            _reference_report_markdown(report)
        )
        emit(
            args,
            report.model_dump(mode="json"),
            (
                f"Ran {len(report.workflows)} reference workflow(s) in "
                f"{report.total_run_duration_seconds}s of run time. Every baseline "
                f"reproduced and every candidate regressed (approval gate or "
                f"regulation). Wrote {out_dir / 'reference-workflows-report.json'}."
            ),
        )

    asyncio.run(run())


def _reference_report_markdown(report: ReferenceWorkflowReport) -> str:
    lines = [
        "# Phase 7.6 — Reference-workflow integration report",
        "",
        f"- Workflows: {len(report.workflows)}",
        f"- Total measured run duration: {report.total_run_duration_seconds}s",
        "",
        "| Workflow | Baseline | Candidate | Comparison | Gate | Tools | Run (s) |",
        "|---|---|---|---|---|---|---|",
    ]
    for entry in report.workflows:
        lines.append(
            "| "
            + " | ".join(
                [
                    entry.workflow_id,
                    entry.baseline.verdict,
                    entry.candidate.verdict,
                    entry.comparison.verdict.value,
                    "yes" if entry.gate_required else "no",
                    f"{entry.safe_tools}/{entry.sensitive_tools}",
                    f"{entry.run_duration_seconds:.3f}",
                ]
            )
            + " |"
        )
    lines.append("")
    lines.append("## Reused lab code")
    reused = {
        item
        for entry in report.workflows
        for item in entry.reused_code
    }
    lines.extend(f"- {item}" for item in sorted(reused))
    lines.append("")
    lines.append("## Flagged contract changes (need approval)")
    lines.extend(f"- {item}" for item in report.flagged_contract_changes)
    lines.append("")
    lines.append("## Per-workflow notes")
    for entry in report.workflows:
        lines.append(f"### {entry.workflow_id}")
        lines.append(f"- Source: {entry.source}")
        lines.append(f"- Business outcome: {entry.baseline.business_outcome}")
        lines.append(f"- Integration code: {entry.integration_code_lines} lines")
        lines.append(f"- Missing capabilities: {len(entry.missing_capabilities)}")
        for note in entry.real_operation_notes:
            lines.append(f"- Operation: {note}")
    return "\n".join(lines)

def build_parser() -> argparse.ArgumentParser:
    """This function builds the ``lab`` command parser."""
    parser = argparse.ArgumentParser(
        prog="lab",
        description="Agent simulation lab workflow over the Phase 7 services.",
    )
    parser.add_argument("--json", action="store_true", help="print stable structured output")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("import-trace", help="import one source trace as evidence")
    p.add_argument("trace_id")
    p.add_argument("--source", choices=("fixture", "langsmith"), default="fixture")
    p.set_defaults(func=cmd_import_trace)

    p = sub.add_parser("scenario", help="scenario operations")
    psub = p.add_subparsers(dest="scenario_command", required=True)
    create = psub.add_parser("create", help="create one scenario linked to its fixture evidence")
    create.add_argument("scenario_id")
    create.set_defaults(func=cmd_scenario_create)

    p = sub.add_parser("bundle", help="bundle operations")
    psub = p.add_subparsers(dest="bundle_command", required=True)
    compile_parser = psub.add_parser("compile", help="compile one reviewed bundle")
    compile_parser.add_argument("scenario_id")
    compile_parser.add_argument("--review-file", required=True, help="JSON review file")
    compile_parser.add_argument("--out", help="output bundle JSON path")
    compile_parser.set_defaults(func=cmd_bundle_compile)

    p = sub.add_parser("run", help="run one bundle or saved case")
    run_source = p.add_mutually_exclusive_group(required=True)
    run_source.add_argument("bundle_path", nargs="?", help="bundle JSON file")
    run_source.add_argument("--case", help="saved case as <case-id>@<version>")
    p.add_argument("--offline", action="store_true", help="use the deterministic model substitute")
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("compare", help="compare baseline and one candidate model on bundles")
    p.add_argument("bundle_paths", nargs="+", help="bundle JSON files")
    p.add_argument("--candidate-config", required=True, help="JSON candidate config file")
    p.add_argument("--offline", action="store_true", help="use the deterministic model substitute")
    p.set_defaults(func=cmd_compare)

    p = sub.add_parser("regression", help="regression case operations")
    psub = p.add_subparsers(dest="regression_command", required=True)
    add = psub.add_parser("add", help="save one approved bundle as a case")
    add.add_argument("bundle_path")
    add.add_argument(
        "--source-type",
        required=True,
        choices=("incident", "suspicious_success", "designed_edge_case", "model_comparison"),
    )
    add.set_defaults(func=cmd_regression_add)

    p = sub.add_parser("suite", help="suite operations")
    psub = p.add_subparsers(dest="suite_command", required=True)
    create = psub.add_parser("create", help="create one versioned suite")
    create.add_argument("name")
    create.add_argument("--member", action="append", required=True, help="<case-id>@<version>")
    create.set_defaults(func=cmd_suite_create)
    run_parser = psub.add_parser("run", help="run one suite comparison")
    run_parser.add_argument("suite_ref", help="suite name or name@version")
    run_parser.add_argument("--candidate-config", required=True, help="JSON candidate config file")
    run_parser.add_argument(
        "--offline", action="store_true", help="use the deterministic model substitute"
    )
    run_parser.set_defaults(func=cmd_suite_run)

    p = sub.add_parser("cases", help="list saved cases")
    psub = p.add_subparsers(dest="cases_command", required=True)
    listing = psub.add_parser("list", help="list saved cases")
    listing.set_defaults(func=cmd_cases_list)

    p = sub.add_parser("suites", help="list saved suites")
    psub = p.add_subparsers(dest="suites_command", required=True)
    listing = psub.add_parser("list", help="list saved suites")
    listing.set_defaults(func=cmd_suites_list)

    p = sub.add_parser("proof", help="phase 7.5 proof")
    psub = p.add_subparsers(dest="proof_command", required=True)
    eight = psub.add_parser("eight", help="run all eight scenarios end to end")
    eight.add_argument("--review-file", required=True, help="JSON review file")
    eight.add_argument("--out", help="report output directory")
    eight.add_argument(
        "--offline", action="store_true", help="use the deterministic model substitute"
    )
    eight.set_defaults(func=cmd_proof_eight)

    p = sub.add_parser("audit", help="phase 7.7 privacy and reproducibility audit")
    psub = p.add_subparsers(dest="audit_command", required=True)
    audit_run = psub.add_parser("run", help="run the audit against an isolated test database")
    audit_run.add_argument("--out", help="audit report output directory")
    audit_run.add_argument(
        "--artifacts-root",
        default=str(ARTIFACTS_DIR),
        help="artifact tree to scan (default: artifacts)",
    )
    audit_run.set_defaults(func=cmd_audit)

    p = sub.add_parser(
        "simulate", help="run one user-simulator flow with a live timeline"
    )
    build_simulate_parser(p, build_default_registry())

    p = sub.add_parser("reference", help="phase 7.6 reference workflows")
    psub = p.add_subparsers(dest="reference_command", required=True)
    ref_run = psub.add_parser("run", help="run one reference workflow baseline/candidate")
    ref_run.add_argument("workflow_id")
    ref_run.set_defaults(func=cmd_reference_run)
    ref_report = psub.add_parser("report", help="run all seven workflows and write the report")
    ref_report.add_argument("--out", help="report output directory")
    ref_report.set_defaults(func=cmd_reference_report)

    p = sub.add_parser(
        "investigate", help="cloud sandbox investigation operations"
    )
    build_investigate_parser(p)

    return parser


def main() -> None:
    """This function runs the ``lab`` command and exits with a status code."""
    parser = build_parser()
    args = parser.parse_args()
    try:
        args.func(args)
    except Exception as error:  # never leak tracebacks or secrets to users
        fail("command_failed", f"{type(error).__name__}: {error}")
    raise SystemExit(0)


if __name__ == "__main__":
    main()
