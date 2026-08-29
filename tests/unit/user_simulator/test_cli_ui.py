"""Focused tests for the generic simulate CLI and setup wizard.

The fake third-party plugin + its YAML manifest prove the CLI is generic: a
new plugin appears in the chooser/listing and runs with zero CLI edits.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml  # type: ignore[import-untyped]

from app.cli import simulate
from app.domain.user_simulator.events import (
    EventEmitter,
    EventKind,
    EventSource,
)
from app.domain.user_simulator.flows import (
    FlowMetadata,
    FlowRegistry,
    FlowRunRequest,
    FlowRunResult,
)
from app.domain.user_simulator.models import SimulatorReport

FLOW_ID = "fake-test-flow"

MANIFEST_YAML = {
    "schema_version": "1.0",
    "catalog_id": "fake-catalog",
    "name": "Fake catalog",
    "group": "custom",
    "scenarios": [
        {
            "scenario_id": FLOW_ID,
            "plugin_id": FLOW_ID,
            "name": "Fake test flow",
            "description": "A third-party flow that needs no CLI edits.",
            "persona": "A concise test customer",
            "script": "Please check my order",
            "goal": "resolved",
            "max_turns": 3,
            "environment_profile": "lab-test-pg",
        }
    ],
}


@pytest.fixture
def catalog_dir(tmp_path: Path) -> Path:
    """A catalog directory with only the third-party manifest.

    Environment profiles come from the loader's own config file, so the
    manifest references the shipped ``lab-test-pg`` profile.
    """
    (tmp_path / "simulations").mkdir()
    (tmp_path / "simulations" / "fake.yaml").write_text(
        yaml.safe_dump(MANIFEST_YAML, sort_keys=False), encoding="utf-8"
    )
    return tmp_path


class _FakePlugin:
    """A minimal third-party flow with its own async engine."""

    metadata = FlowMetadata(
        flow_id=FLOW_ID, case_id=FLOW_ID, kind="custom", name="Fake test flow"
    )

    def __init__(self, *, interrupt: bool = False) -> None:
        self.requests: list[FlowRunRequest] = []
        self._interrupt = interrupt

    async def _engine(self, request: FlowRunRequest) -> FlowRunResult:
        sinks = [request.sink] if request.sink is not None else []
        emitter = EventEmitter("run-xyz", self.metadata.case_id, sinks)
        emitter.emit(
            EventKind.START,
            EventSource.ENGINE,
            text="starting",
            detail=(
                "run_id=run-xyz",
                f"jsonl_path={request.root / 'run-xyz.jsonl'}",
                f"report_path={request.root / 'run-xyz.json'}",
            ),
        )
        emitter.emit(EventKind.USER, EventSource.PERSONA, text="Please check my order", turn=1)
        emitter.emit(
            EventKind.TOOL_SELECTED,
            EventSource.SUPPORT,
            text="get_order_status(order_id=o1)",
            tool="get_order_status",
            outcome="selected",
        )
        emitter.emit(
            EventKind.MODEL,
            EventSource.SUPPORT,
            text="model support_agent.answer tokens=5",
            model_provider="fake",
            model_name="fake",
            tokens=5,
            latency_ms=12.0,
        )
        emitter.emit(
            EventKind.TOOL_RESULT,
            EventSource.SUPPORT,
            text="get_order_status(order_id=o1) ok",
            tool="get_order_status",
            outcome="ok",
        )
        emitter.emit(EventKind.AGENT, EventSource.SUPPORT, text="Your order was delivered", turn=1)
        emitter.emit(
            EventKind.STATE, EventSource.SUPPORT, text="state: order:delivered",
            transition="order:delivered",
        )
        emitter.emit(
            EventKind.DONE,
            EventSource.SUPPORT,
            text="done",
            outcome="state_verified_success",
            verified=True,
            turns=1,
            tokens=5,
            latency_ms=12.0,
        )
        return FlowRunResult(
            report=SimulatorReport(
                run_id="run-xyz",
                case_id=self.metadata.case_id,
                kind="custom",
                model_provider="fake",
                model_name="fake",
                end_reason="state_verified_success",
                turns=1,
                verified_goal=True,
                total_tokens=5,
                total_latency_ms=12.0,
                cost_usd=0.01,
            ),
            transcript_path=request.root / "run-xyz.jsonl",
            report_path=request.root / "run-xyz.json",
        )

    async def run(self, request: FlowRunRequest) -> FlowRunResult:
        self.requests.append(request)
        if self._interrupt:
            # Simulate an interrupt after the run started: START was emitted.
            if request.sink is not None:
                start = EventEmitter(
                    "run-xyz", self.metadata.case_id, [request.sink]
                )
                start.emit(
                    EventKind.START,
                    EventSource.ENGINE,
                    text="starting",
                    detail=("run_id=run-xyz",),
                )
            raise KeyboardInterrupt
        return await self._engine(request)


def _registry(*plugins: object) -> FlowRegistry:
    registry = FlowRegistry()
    for plugin in plugins:
        registry.register(plugin)  # type: ignore[arg-type]
    return registry


def _run(
    argv: list[str],
    registry: FlowRegistry,
    catalog_dir: Path,
    *,
    live: bool = False,
) -> int:
    return simulate.run_simulate(
        argv,
        registry=registry,
        live=live,
        catalog_root=catalog_dir / "simulations",
    )


async def _ok_preflight(**kwargs: object) -> tuple[()]:
    del kwargs
    return ()


def test_third_party_plugin_with_yaml_appears_in_list_without_cli_edits(
    catalog_dir: Path, capsys
) -> None:
    registry = _registry(_FakePlugin())
    code = _run(["simulate", "list"], registry, catalog_dir)
    captured = capsys.readouterr()
    assert code == 0
    assert FLOW_ID in captured.out
    assert "custom" in captured.out


def test_plain_mode_prints_one_line_per_event(catalog_dir: Path, capsys, monkeypatch) -> None:
    registry = _registry(_FakePlugin())
    monkeypatch.setattr(simulate, "run_preflight", _ok_preflight)
    code = _run(
        ["simulate", "run", FLOW_ID, "--max-turns", "3"],
        registry,
        catalog_dir,
    )
    captured = capsys.readouterr()
    assert code == 0
    lines = captured.out.splitlines()
    assert "run_id=run-xyz" in lines
    assert "jsonl_path=artifacts/user-simulator/run-xyz.jsonl" in lines
    assert "[1] ENGINE start starting" in lines
    assert "[2] PERSONA user Please check my order" in lines
    model_line = "[4] SUPPORT model (reasoning unavailable) · model support_agent.answer tokens=5"
    assert model_line in lines
    assert "[5] SUPPORT tool result get_order_status(order_id=o1) ok" in lines
    assert "[7] SUPPORT state state: order:delivered" in lines
    assert "status: state_verified_success (verified) · 1 turns · 5 tokens · cost $0.0100" in lines
    assert "report_path=artifacts/user-simulator/run-xyz.json" in lines


def test_rich_mode_renders_header_labels_and_values(
    catalog_dir: Path, capsys, monkeypatch
) -> None:
    registry = _registry(_FakePlugin())
    monkeypatch.setattr(simulate, "run_preflight", _ok_preflight)
    code = _run(["simulate", "run", FLOW_ID], registry, catalog_dir, live=True)
    captured = capsys.readouterr()
    assert code == 0
    out = captured.out
    assert "USER SIMULATOR" in out
    assert "Fake test flow" in out
    assert "RUN  run-xyz" in out
    assert "tool selected" in out
    assert "tool result" in out
    assert "user" in out
    assert "(reasoning unavailable)" in out
    assert "status: state_verified_success (verified)" in out


def test_rich_mode_truncates_values_to_fit_80_columns(
    catalog_dir: Path, capsys, monkeypatch
) -> None:
    import re

    registry = _registry(_FakePlugin())
    monkeypatch.setattr(simulate, "run_preflight", _ok_preflight)
    code = _run(["simulate", "run", FLOW_ID], registry, catalog_dir, live=True)
    captured = capsys.readouterr()
    assert code == 0
    ansi = re.compile(r"\x1b\[[0-9;]*m")
    visible = [ansi.sub("", line) for line in captured.out.splitlines() if line.strip()]
    assert max(len(line) for line in visible) <= 80


def test_rich_timeline_adapts_to_a_narrow_terminal(capsys) -> None:
    from rich.console import Console

    console = Console(width=48, highlight=False)
    sink = simulate.RichTimelineSink(_FakePlugin.metadata, console=console)
    emitter = EventEmitter("run-narrow", FLOW_ID, [sink])
    emitter.emit(
        EventKind.START,
        EventSource.ENGINE,
        text="starting",
        detail=("run_id=run-narrow", "jsonl_path=a.jsonl", "report_path=a.json"),
    )
    emitter.emit(
        EventKind.AGENT,
        EventSource.SUPPORT,
        text="A long response that must fit without breaking the terminal layout",
    )

    visible = capsys.readouterr().out.splitlines()
    assert max(len(line) for line in visible) <= 48
    assert any("agent" in line and "support" in line for line in visible)


def test_missing_simulation_fails_with_available_list(
    catalog_dir: Path, capsys
) -> None:
    registry = _registry(_FakePlugin())
    with pytest.raises(SystemExit) as excinfo:
        _run(["simulate", "run", "missing"], registry, catalog_dir)
    assert excinfo.value.code == 1
    captured = capsys.readouterr()
    assert "simulation_not_found" in captured.err
    assert FLOW_ID in captured.err


def test_no_simulation_id_without_a_terminal_fails(catalog_dir: Path, capsys) -> None:
    registry = _registry(_FakePlugin())
    with pytest.raises(SystemExit) as excinfo:
        _run(["simulate", "run"], registry, catalog_dir)
    assert excinfo.value.code == 1
    assert "simulation_required" in capsys.readouterr().err


def test_bare_simulate_launches_workbench(catalog_dir: Path, monkeypatch) -> None:
    registry = _registry(_FakePlugin())
    workbench_called = False

    async def _fake_workbench(catalog, reg):
        nonlocal workbench_called
        workbench_called = True
        return None

    monkeypatch.setattr(
        "app.cli.textual_simulate.run_interactive_workbench", _fake_workbench
    )
    monkeypatch.setattr("sys.stdout.isatty", lambda: True)
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)

    code = _run(["simulate"], registry, catalog_dir, live=True)
    assert code == 0
    assert workbench_called is True


def test_request_flags_reach_the_plugin(catalog_dir: Path, monkeypatch) -> None:
    plugin = _FakePlugin()
    registry = _registry(plugin)
    monkeypatch.setattr(simulate, "run_preflight", _ok_preflight)
    _run(
        ["simulate", "run", FLOW_ID, "--max-turns", "7", "--no-live"],
        registry,
        catalog_dir,
    )
    request = plugin.requests[0]
    assert request.max_turns == 7
    assert isinstance(request.sink, simulate.PlainEventSink)


def test_json_output_prints_the_stable_report(catalog_dir: Path, capsys, monkeypatch) -> None:
    registry = _registry(_FakePlugin())
    monkeypatch.setattr(simulate, "run_preflight", _ok_preflight)
    code = _run(["--json", "simulate", "run", FLOW_ID], registry, catalog_dir)
    captured = capsys.readouterr()
    assert code == 0
    payload = json.loads(captured.out)
    assert payload["run_id"] == "run-xyz"
    assert payload["end_reason"] == "state_verified_success"


def test_preflight_failure_stops_before_any_artifact(
    catalog_dir: Path, capsys, monkeypatch
) -> None:
    from app.domain.user_simulator.preflight import PreflightIssue

    registry = _registry(_FakePlugin())

    async def failing_preflight(**kwargs: object) -> tuple[PreflightIssue, ...]:
        del kwargs
        return (
            PreflightIssue(
                "environment",
                "ENVIRONMENT must be 'test'",
                "export ENVIRONMENT=test or set it in .env",
            ),
        )

    monkeypatch.setattr(simulate, "run_preflight", failing_preflight)
    code = _run(["simulate", "run", FLOW_ID], registry, catalog_dir)
    captured = capsys.readouterr()
    assert code == 1
    assert "setup check failed; nothing was started" in captured.err
    assert "ENVIRONMENT must be 'test'" in captured.err
    assert "run_id=" not in captured.out  # no run id was allocated


def test_setup_cancel_creates_no_artifacts(catalog_dir: Path, capsys, monkeypatch) -> None:
    plugin = _FakePlugin()
    registry = _registry(plugin)
    monkeypatch.setattr(simulate, "run_preflight", _ok_preflight)
    monkeypatch.setattr(simulate, "_interactive", lambda args, live: True)
    monkeypatch.setattr(simulate, "_choose_scenario", lambda console, catalog: None)
    code = _run(["simulate", "run"], registry, catalog_dir, live=True)
    captured = capsys.readouterr()
    assert code == 0
    assert "setup cancelled; no artifacts were created" in captured.out
    assert plugin.requests == []


def test_validate_reports_yaml_issues(catalog_dir: Path, capsys) -> None:
    (catalog_dir / "simulations" / "bad.yaml").write_text(
        yaml.safe_dump(
            {
                "schema_version": "1.0",
                "catalog_id": "bad-catalog",
                "group": "custom",
                "scenarios": [
                    {
                        "scenario_id": "bad-one",
                        "plugin_id": "not-registered",
                        "name": "Bad flow",
                        "environment_profile": "lab-test-pg",
                    }
                ],
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    registry = _registry(_FakePlugin())
    code = _run(["simulate", "validate"], registry, catalog_dir)
    captured = capsys.readouterr()
    assert code == 1
    assert "bad.yaml" in captured.err
    assert "unknown plugin id 'not-registered'" in captured.err


def test_persona_overrides_reach_an_unrelated_test_plugin(
    catalog_dir: Path, capsys, monkeypatch
) -> None:
    plugin = _FakePlugin()
    registry = _registry(plugin)
    monkeypatch.setattr(simulate, "run_preflight", _ok_preflight)
    code = _run(
        [
            "simulate", "run", FLOW_ID, "--yes",
            "--persona", "A very different customer",
            "--script", "Please check my other order",
            "--goal", "fully resolved",
        ],
        registry,
        catalog_dir,
    )
    assert code == 0
    request = plugin.requests[0]
    assert request.persona_context == "A very different customer"
    assert request.script == "Please check my other order"
    assert request.goal == "fully resolved"


def test_selected_profile_url_reaches_the_run(
    catalog_dir: Path, monkeypatch
) -> None:
    """The configured DATABASE_URL reaches the plugin runtime."""
    monkeypatch.setenv("DATABASE_URL", "postgresql://lab:lab@127.0.0.1:5432/lab")
    plugin = _FakePlugin()
    registry = _registry(plugin)
    monkeypatch.setattr(simulate, "run_preflight", _ok_preflight)
    code = _run(["simulate", "run", FLOW_ID, "--yes"], registry, catalog_dir)
    assert code == 0
    request = plugin.requests[0]
    assert request.runtime is not None
    assert request.runtime.database_url == "postgresql://lab:lab@127.0.0.1:5432/lab"
    assert request.runtime.environment == "test"


def test_keyboard_interrupt_reports_observed_cleanup_status(
    catalog_dir: Path, capsys, monkeypatch, tmp_path
) -> None:
    registry = _registry(_FakePlugin(interrupt=True))
    monkeypatch.setattr(simulate, "run_preflight", _ok_preflight)
    code = _run(["simulate", "run", FLOW_ID, "--yes"], registry, catalog_dir)
    captured = capsys.readouterr()
    assert code == 130
    assert "interrupted" in captured.err.lower()
    assert "cleanup status unknown" in captured.err.lower()


def test_interrupt_with_observed_cleanup_event_reports_complete(
    catalog_dir: Path, capsys, monkeypatch, tmp_path
) -> None:
    import json as jsonlib

    plugin = _FakePlugin(interrupt=True)
    registry = _registry(plugin)
    monkeypatch.setattr(simulate, "run_preflight", _ok_preflight)
    # The engine writes the partial report BEFORE cleanup; completeness is
    # proven only by the cleanup event the engine emits after rollback.
    root = Path("artifacts/user-simulator")
    root.mkdir(parents=True, exist_ok=True)
    partial = root / "run-xyz.json"
    partial.write_text(jsonlib.dumps({"end_reason": "cancelled"}), encoding="utf-8")
    jsonl = root / "run-xyz.jsonl"
    jsonl.write_text(
        jsonlib.dumps({"event": "cleanup", "reason": "rollback"}) + "\n",
        encoding="utf-8",
    )
    try:
        code = _run(["simulate", "run", FLOW_ID, "--yes"], registry, catalog_dir)
        captured = capsys.readouterr()
        assert code == 130
        assert "cleanup and rollback complete" in captured.err.lower()
    finally:
        partial.unlink(missing_ok=True)
        jsonl.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Inspector proofs: documented commands, cleanup truthfulness, --json modes
# ---------------------------------------------------------------------------


def test_documented_primary_commands_parse_and_run_to_their_boundary(
    catalog_dir: Path, capsys, monkeypatch
) -> None:
    """README/scripts/docs command forms parse and reach their boundary."""
    registry = _registry(_FakePlugin())
    # lab simulate list
    code = simulate.run_simulate(
        ["simulate", "list"],
        registry=registry,
        catalog_root=catalog_dir / "simulations",
        live=False,
    )
    assert code == 0
    assert FLOW_ID in capsys.readouterr().out
    # lab simulate run <id> --yes
    monkeypatch.setattr(simulate, "run_preflight", _ok_preflight)
    code = simulate.run_simulate(
        ["simulate", "run", FLOW_ID, "--yes", "--no-live"],
        registry=registry,
        catalog_root=catalog_dir / "simulations",
        live=False,
    )
    assert code == 0
    # the real lab parser dispatches both forms to the intended subcommand
    from app.cli.main import build_parser

    parser = build_parser()
    listed = parser.parse_args(["simulate", "list"])
    assert listed.simulate_command == "list"
    ran = parser.parse_args(
        ["simulate", "run", "phase2-01-bad-prompt-policy-answer", "--yes", "--no-live"]
    )
    assert ran.simulate_command == "run"
    assert ran.simulation_id == "phase2-01-bad-prompt-policy-answer"


def _interrupt_args(
    tmp_path: Path, *, json_mode: bool, run_id: str | None = "run-xyz"
) -> object:
    from types import SimpleNamespace

    sink = simulate.PlainEventSink()
    sink.run_id = run_id
    return SimpleNamespace(json=json_mode, _sink=sink, _artifact_root=str(tmp_path))


def test_interrupt_successful_cleanup_reports_observed_complete(tmp_path: Path, capsys) -> None:
    import json as jsonlib

    (tmp_path / "run-xyz.jsonl").write_text(
        jsonlib.dumps({"event": "cleanup", "reason": "rollback"}) + "\n",
        encoding="utf-8",
    )
    args = _interrupt_args(tmp_path, json_mode=False)
    code = simulate._interrupt_exit(args)
    captured = capsys.readouterr()
    assert code == 130
    assert "cleanup and rollback complete" in captured.err.lower()
    assert "(observed)" in captured.err.lower()


def test_interrupt_report_exists_but_cleanup_failed(tmp_path: Path, capsys) -> None:
    import json as jsonlib

    # The partial report exists (written before cleanup) but the engine's
    # cleanup event says cleanup_failed -> must NOT claim completion.
    (tmp_path / "run-xyz.json").write_text(
        jsonlib.dumps({"end_reason": "cancelled"}), encoding="utf-8"
    )
    (tmp_path / "run-xyz.jsonl").write_text(
        jsonlib.dumps({"event": "cleanup", "reason": "cleanup_failed"}) + "\n",
        encoding="utf-8",
    )
    args = _interrupt_args(tmp_path, json_mode=False)
    code = simulate._interrupt_exit(args)
    captured = capsys.readouterr()
    assert code == 1
    assert "cleanup failed" in captured.err.lower()
    assert "complete" not in captured.err.lower()


def test_interrupt_no_cleanup_evidence_reports_unknown(tmp_path: Path, capsys) -> None:
    import json as jsonlib

    # Events exist but no cleanup event: cleanup cannot be claimed.
    (tmp_path / "run-xyz.jsonl").write_text(
        jsonlib.dumps({"event": "done", "outcome": "cancelled"}) + "\n",
        encoding="utf-8",
    )
    args = _interrupt_args(tmp_path, json_mode=False)
    code = simulate._interrupt_exit(args)
    captured = capsys.readouterr()
    assert code == 130
    assert "cleanup status unknown" in captured.err.lower()


def test_interrupt_json_mode_prints_structured_error(tmp_path: Path, capsys) -> None:
    import json as jsonlib

    (tmp_path / "run-xyz.jsonl").write_text(
        jsonlib.dumps({"event": "cleanup", "reason": "rollback"}) + "\n",
        encoding="utf-8",
    )
    args = _interrupt_args(tmp_path, json_mode=True)
    code = simulate._interrupt_exit(args)
    captured = capsys.readouterr()
    assert code == 130
    payload = json.loads(captured.out)
    assert payload["ok"] is False
    assert payload["error"]["code"] == "interrupted"
    assert payload["error"]["cleanup"] == "rollback"
    assert captured.err == ""  # nothing plain on stderr


def test_json_flag_before_and_after_simulate_parse(catalog_dir: Path, monkeypatch, capsys) -> None:
    import json as jsonlib

    registry = _registry(_FakePlugin())
    monkeypatch.setattr(simulate, "run_preflight", _ok_preflight)
    code = _run(["--json", "simulate", "run", FLOW_ID], registry, catalog_dir)
    assert code == 0
    payload = jsonlib.loads(capsys.readouterr().out)
    assert payload["run_id"] == "run-xyz"
    code = _run(["simulate", "run", FLOW_ID, "--json"], registry, catalog_dir)
    assert code == 0
    payload = jsonlib.loads(capsys.readouterr().out)
    assert payload["run_id"] == "run-xyz"


def test_json_mode_missing_id_prints_structured_error(
    catalog_dir: Path, capsys
) -> None:
    import json as jsonlib

    registry = _registry(_FakePlugin())
    for argv in (["--json", "simulate", "run"], ["simulate", "run", "--json"]):
        with pytest.raises(SystemExit) as excinfo:
            _run(argv, registry, catalog_dir)
        assert excinfo.value.code == 1
        captured = capsys.readouterr()
        payload = jsonlib.loads(captured.out)
        assert payload["ok"] is False
        assert payload["error"]["code"] == "simulation_required"
        assert captured.err == ""  # no plain error text mixed in


def test_json_mode_missing_scenario_prints_structured_error(
    catalog_dir: Path, capsys
) -> None:
    import json as jsonlib

    registry = _registry(_FakePlugin())
    with pytest.raises(SystemExit) as excinfo:
        _run(["simulate", "run", "nope", "--json"], registry, catalog_dir)
    assert excinfo.value.code == 1
    captured = capsys.readouterr()
    payload = jsonlib.loads(captured.out)
    assert payload["ok"] is False
    assert payload["error"]["code"] == "simulation_not_found"
    assert captured.err == ""


def test_list_json_three_positions_emit_stable_machine_output(capsys) -> None:
    """`lab [--json] simulate [--json] list [--json]` emits one JSON object."""
    import json as jsonlib

    from app.domain.user_simulator.plugins import build_default_registry

    repo = Path(__file__).resolve().parents[3] / "simulations"
    registry = build_default_registry()
    required = {
        "scenario_id",
        "plugin_id",
        "group",
        "name",
        "description",
        "max_turns",
        "environment_profile",
    }
    for argv in (
        ["--json", "simulate", "list"],
        ["simulate", "--json", "list"],
        ["simulate", "list", "--json"],
    ):
        code = simulate.run_simulate(
            argv, registry=registry, catalog_root=repo, live=False
        )
        captured = capsys.readouterr()
        assert code == 0
        payload = jsonlib.loads(captured.out)  # exact parseability: pure JSON
        assert payload["ok"] is True
        assert len(payload["simulations"]) == 15
        assert set(payload["catalogs"]) == {"support", "reference"}
        assert payload["catalog_count"] == 2
        assert required <= set(payload["simulations"][0])
        assert captured.err == ""  # empty stderr


def test_list_plain_output_unchanged_without_json(catalog_dir: Path, capsys) -> None:
    """Without --json the plain grouped listing is preserved."""
    registry = _registry(_FakePlugin())
    code = _run(["simulate", "list"], registry, catalog_dir)
    captured = capsys.readouterr()
    assert code == 0
    assert "no simulations configured" not in captured.out
    assert FLOW_ID in captured.out
    assert "[custom]" in captured.out
