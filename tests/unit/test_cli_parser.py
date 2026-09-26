"""CLI parser tests for commands that must reject incomplete or unknown input."""

import pytest

from app.cli.experiment import ExperimentCliError, _prompt_runtime
from app.cli.main import build_parser


def test_run_requires_exactly_one_input_source() -> None:
    parser = build_parser()

    with pytest.raises(SystemExit):
        parser.parse_args(["run"])
    with pytest.raises(SystemExit):
        parser.parse_args(["run", "bundle.json", "--case", "case-id@1"])

    assert parser.parse_args(["run", "bundle.json"]).bundle_path == "bundle.json"
    assert parser.parse_args(["run", "--case", "case-id@1"]).case == "case-id@1"


@pytest.mark.parametrize("command", ["cases", "suites"])
def test_list_commands_reject_unknown_operations(command: str) -> None:
    parser = build_parser()

    with pytest.raises(SystemExit):
        parser.parse_args([command, "typo"])

    assert parser.parse_args([command, "list"]).func is not None


def test_simulate_defaults_and_programmatic_flags() -> None:
    parser = build_parser()

    default_run = parser.parse_args(["simulate"])
    assert default_run.simulate_command == "run"
    assert default_run.simulation_id is None

    explicit_run = parser.parse_args(
        ["simulate", "run", "phase2-03-database-timeout", "--max-turns", "5", "--yes"]
    )
    assert explicit_run.simulation_id == "phase2-03-database-timeout"
    assert explicit_run.max_turns == 5
    assert explicit_run.yes is True


def test_experiment_parser_accepts_create_and_synchronous_start() -> None:
    parser = build_parser()
    case_id = "e693cb4c-98a7-5d3d-bd7a-1c0c554ab528"

    create = parser.parse_args(
        [
            "experiment",
            "create",
            "--case",
            f"{case_id}@1",
            "--candidate-change",
            "model",
            "--baseline-prompt-file",
            "baseline.txt",
            "--seed",
            "7",
        ]
    )
    assert create.experiment_command == "create"
    assert create.repetitions == 3

    start = parser.parse_args(
        [
            "experiment",
            "start",
            case_id,
            "--baseline-prompt-file",
            "baseline.txt",
        ]
    )
    assert start.experiment_command == "start"
    assert str(start.experiment_id) == case_id


def test_experiment_cli_rejects_an_empty_prompt_file(tmp_path) -> None:
    prompt_file = tmp_path / "empty-prompt.txt"
    prompt_file.write_text("  \n", encoding="utf-8")

    with pytest.raises(ExperimentCliError) as raised:
        _prompt_runtime(prompt_file, "support-answer", "1.0.0")

    assert raised.value.code == "experiment_prompt_invalid"
