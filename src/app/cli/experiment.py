"""CLI delivery for the synchronous support-only experiment operator path."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from typing import Any, Coroutine, TypeVar
from uuid import UUID

from pydantic import SecretStr, ValidationError

from app.config import Settings, get_settings
from app.db import get_session_factory
from app.domain.experiment.contracts import CandidateChangeType, ExecutionLimits
from app.domain.experiment.events import ExperimentEvent
from app.domain.experiment.models import ExperimentRecord
from app.domain.experiment.operator import (
    SupportExperimentDraft,
    SupportExperimentOperator,
    SupportExperimentStartInputs,
    SupportPromptReference,
    SupportPromptRuntime,
    SupportSuiteExperimentDraft,
)
from app.domain.experiment.support_results import SupportResult
from app.errors import DomainError

ResultT = TypeVar("ResultT")


class ExperimentCliError(Exception):
    """A safe, stable command-line failure."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def build_experiment_parser(parser: argparse.ArgumentParser) -> None:
    """Add `lab experiment` commands that share the API operator path."""
    sub = parser.add_subparsers(dest="experiment_command", required=True)

    create = sub.add_parser("create", help="create a pending support experiment")
    _add_create_arguments(create)
    create.set_defaults(func=cmd_experiment_create)

    start = sub.add_parser(
        "start",
        help="run every repetition synchronously",
    )
    start.add_argument("experiment_id", type=UUID)
    _add_runtime_prompt_arguments(start)
    start.set_defaults(func=cmd_experiment_start)

    status_parser = sub.add_parser("status", help="show durable experiment status")
    status_parser.add_argument("experiment_id", type=UUID)
    status_parser.set_defaults(func=cmd_experiment_status)

    replay = sub.add_parser("replay", help="replay persisted ordered events")
    replay.add_argument("experiment_id", type=UUID)
    replay.add_argument("--after-sequence", type=int, default=0)
    replay.add_argument("--limit", type=int, default=100)
    replay.set_defaults(func=cmd_experiment_replay)

    cancel = sub.add_parser("cancel", help="cancel a pending experiment")
    cancel.add_argument("experiment_id", type=UUID)
    cancel.set_defaults(func=cmd_experiment_cancel)

    result = sub.add_parser("result", help="export a verified complete result")
    result.add_argument("experiment_id", type=UUID)
    result.add_argument("--out", type=Path, help="write verified JSON to this file")
    result.set_defaults(func=cmd_experiment_result)


def _add_create_arguments(parser: argparse.ArgumentParser) -> None:
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--case", help="<case-id>@<case-version>")
    target.add_argument("--suite", help="<suite-id>@<suite-version>")
    parser.add_argument(
        "--candidate-change",
        required=True,
        choices=tuple(change.value for change in CandidateChangeType),
    )
    _add_prompt_reference_arguments(parser)
    parser.add_argument("--repetitions", type=int, default=3)
    parser.add_argument("--seed", type=int, required=True)
    parser.add_argument("--max-duration-s", type=int, default=300)


def _add_prompt_reference_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--baseline-prompt-file", type=Path, required=True)
    parser.add_argument("--baseline-prompt-id", default="support-answer")
    parser.add_argument("--baseline-prompt-version", default="1.0.0")
    parser.add_argument("--candidate-prompt-file", type=Path)
    parser.add_argument("--candidate-prompt-id", default="support-answer")
    parser.add_argument("--candidate-prompt-version", default="1.0.0")


def _add_runtime_prompt_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--baseline-prompt-file", type=Path, required=True)
    parser.add_argument("--baseline-prompt-id", default="support-answer")
    parser.add_argument("--baseline-prompt-version", default="1.0.0")
    parser.add_argument("--candidate-prompt-file", type=Path)
    parser.add_argument("--candidate-prompt-id", default="support-answer")
    parser.add_argument("--candidate-prompt-version", default="1.0.0")


def cmd_experiment_create(args: argparse.Namespace) -> None:
    """Create a pending contract; prompt content is hashed and never persisted."""
    try:
        baseline = _prompt_reference(
            args.baseline_prompt_file,
            args.baseline_prompt_id,
            args.baseline_prompt_version,
        )
        candidate = (
            _prompt_reference(
                args.candidate_prompt_file,
                args.candidate_prompt_id,
                args.candidate_prompt_version,
            )
            if args.candidate_prompt_file is not None
            else baseline
        )
        if args.case is not None:
            case_id, case_version = _case_reference(args.case)
            draft: SupportExperimentDraft | SupportSuiteExperimentDraft = SupportExperimentDraft(
                case_id=case_id,
                case_version=case_version,
                candidate_change=args.candidate_change,
                baseline_prompt=baseline,
                candidate_prompt=candidate,
                limits=ExecutionLimits(max_turns=1, max_duration_s=args.max_duration_s),
                repetitions=args.repetitions,
                seed=args.seed,
            )
        else:
            suite_id, suite_version = _suite_reference(args.suite)
            draft = SupportSuiteExperimentDraft(
                suite_id=suite_id,
                suite_version=suite_version,
                candidate_change=args.candidate_change,
                baseline_prompt=baseline,
                candidate_prompt=candidate,
                limits=ExecutionLimits(max_turns=1, max_duration_s=args.max_duration_s),
                repetitions=args.repetitions,
                seed=args.seed,
            )
    except ValidationError as error:
        raise ExperimentCliError(
            "experiment_input_invalid",
            "Experiment input is invalid",
        ) from error

    async def run() -> ExperimentRecord:
        return await _operator().create(draft)

    record = _run(run())
    _emit(
        args,
        _record_payload(record),
        (
            f"Created pending experiment {record.id}. Start it with "
            f"`lab experiment start {record.id} --baseline-prompt-file ...`."
        ),
    )


def cmd_experiment_start(args: argparse.Namespace) -> None:
    """Run a pending experiment synchronously and return only its durable outcome."""
    inputs = _start_inputs(args)

    async def run() -> ExperimentRecord:
        return await _operator().start(
            args.experiment_id,
            inputs,
            operator_credential=_operator_credential(),
        )

    record = _run(run())
    _emit(
        args,
        _record_payload(record),
        (
            f"Experiment {record.id} finished with status {record.status}. "
            "Execution completed in this command."
        ),
    )


def cmd_experiment_status(args: argparse.Namespace) -> None:
    """Show one experiment's safe lifecycle view."""

    async def run() -> ExperimentRecord:
        return await _operator().get_record(args.experiment_id)

    record = _run(run())
    _emit(args, _record_payload(record), f"Experiment {record.id}: {record.status}.")


def cmd_experiment_replay(args: argparse.Namespace) -> None:
    """Print a JSON-safe ordered event replay."""
    if args.after_sequence < 0 or not 1 <= args.limit <= 1_000:
        raise ExperimentCliError(
            "experiment_input_invalid",
            "Event replay requires a non-negative cursor and a limit from 1 to 1000",
        )

    async def run() -> list[ExperimentEvent]:
        return await _operator().replay(
            args.experiment_id,
            after_sequence=args.after_sequence,
            limit=args.limit,
        )

    events = _run(run())
    payload = [event.model_dump(mode="json") for event in events]
    _emit(args, payload, f"Replayed {len(events)} event(s).")


def cmd_experiment_cancel(args: argparse.Namespace) -> None:
    """Cancel one pending experiment."""

    async def run() -> ExperimentRecord:
        return await _operator().cancel(args.experiment_id)

    record = _run(run())
    _emit(args, _record_payload(record), f"Experiment {record.id} cancelled.")


def cmd_experiment_result(args: argparse.Namespace) -> None:
    """Export only a verified complete result."""

    async def run() -> SupportResult:
        return await _operator().verified_result(args.experiment_id)

    result = _run(run())
    rendered = result.model_dump_json(indent=2)
    if args.out is not None:
        try:
            args.out.parent.mkdir(parents=True, exist_ok=True)
            args.out.write_text(rendered + "\n", encoding="utf-8")
        except OSError as error:
            raise ExperimentCliError(
                "experiment_result_write_failed",
                "Cannot write the requested result file",
            ) from error
    _emit(
        args,
        result.model_dump(mode="json"),
        (
            f"Verified result {result.content_hash} for experiment {args.experiment_id}."
            + (f" Wrote {args.out}." if args.out is not None else "")
        ),
    )


def _operator() -> SupportExperimentOperator:
    return SupportExperimentOperator(
        session_factory=get_session_factory(),
        settings=_settings(),
    )


def _operator_credential() -> SecretStr | None:
    """Load the local operator credential without accepting a command-line secret."""
    return _settings().experiment_operator_token


def _settings() -> Settings:
    try:
        return get_settings()
    except ValidationError as error:
        raise ExperimentCliError(
            "configuration_invalid",
            "Experiment configuration is invalid",
        ) from error


def _case_reference(value: str) -> tuple[UUID, int]:
    try:
        case_id, version = value.split("@", 1)
        parsed_version = int(version)
        if parsed_version < 1:
            raise ValueError
        return UUID(case_id), parsed_version
    except (TypeError, ValueError) as error:
        raise ExperimentCliError(
            "invalid_case_ref",
            "Case must look like <case-id>@<case-version>",
        ) from error


def _suite_reference(value: str) -> tuple[UUID, int]:
    try:
        suite_id, version = value.split("@", 1)
        parsed_version = int(version)
        if parsed_version < 1:
            raise ValueError
        return UUID(suite_id), parsed_version
    except (TypeError, ValueError) as error:
        raise ExperimentCliError(
            "invalid_suite_ref",
            "Suite must look like <suite-id>@<suite-version>",
        ) from error


def _prompt_reference(path: Path, prompt_id: str, version: str) -> SupportPromptReference:
    runtime = _prompt_runtime(path, prompt_id, version)
    return runtime.as_reference()


def _start_inputs(args: argparse.Namespace) -> SupportExperimentStartInputs:
    baseline = _prompt_runtime(
        args.baseline_prompt_file,
        args.baseline_prompt_id,
        args.baseline_prompt_version,
    )
    candidate = (
        _prompt_runtime(
            args.candidate_prompt_file,
            args.candidate_prompt_id,
            args.candidate_prompt_version,
        )
        if args.candidate_prompt_file is not None
        else None
    )
    try:
        return SupportExperimentStartInputs(
            baseline_prompt=baseline,
            candidate_prompt=candidate,
        )
    except ValidationError as error:
        raise ExperimentCliError(
            "experiment_input_invalid",
            "Experiment prompt input is invalid",
        ) from error


def _prompt_runtime(path: Path, prompt_id: str, version: str) -> SupportPromptRuntime:
    try:
        content = path.read_text(encoding="utf-8")
    except OSError as error:
        raise ExperimentCliError(
            "experiment_prompt_load_failed",
            f"Cannot read prompt file {path}",
        ) from error
    if not content.strip():
        raise ExperimentCliError(
            "experiment_prompt_invalid",
            "Prompt files must not be empty",
        )
    try:
        return SupportPromptRuntime(
            prompt_id=prompt_id,
            version=version,
            content=SecretStr(content),
        )
    except ValidationError as error:
        raise ExperimentCliError(
            "experiment_input_invalid",
            "Experiment prompt input is invalid",
        ) from error


def _record_payload(record: ExperimentRecord) -> dict[str, object]:
    return {
        "experiment_id": str(record.id),
        "status": record.status,
        "execution_id": str(record.execution_id) if record.execution_id else None,
        "contract_hash": record.contract_hash,
        "error_code": record.error_code,
        "created_at": record.created_at.isoformat(),
        "started_at": record.started_at.isoformat() if record.started_at else None,
        "finished_at": record.finished_at.isoformat() if record.finished_at else None,
    }


def _run(coroutine: Coroutine[Any, Any, ResultT]) -> ResultT:
    try:
        return asyncio.run(coroutine)
    except DomainError as error:
        raise ExperimentCliError(error.code, error.message) from error
    except ValidationError as error:
        raise ExperimentCliError(
            "experiment_input_invalid",
            "Experiment input is invalid",
        ) from error
    except Exception as error:
        raise ExperimentCliError(
            "experiment_operation_failed",
            "The experiment operation did not complete; inspect durable status before retrying",
        ) from error


def _emit(args: argparse.Namespace, payload: object, human: str) -> None:
    if getattr(args, "json", False):
        print(json.dumps(payload, indent=2, default=str))
    else:
        print(human)
