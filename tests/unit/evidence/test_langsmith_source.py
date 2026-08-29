"""This module tests the LangSmith source adapter for checkpoint 3.3.

Fake client failures map into safe typed errors.
Cohort fetches stay bounded.
Credentials never appear in errors or evidence.
"""

import json
from pathlib import Path

import httpx
import pytest
from pydantic import SecretStr
from tests.fakes.langsmith_client import FakeLangSmithClient

from app.adapters.sources.langsmith import (
    LangSmithSource,
    LangSmithSourceConfig,
    langsmith_run_url,
)
from app.domain.evidence.errors import (
    TraceAuthenticationError,
    TraceNotFound,
    TraceRateLimited,
    TraceSourceUnavailable,
    UnsupportedTrace,
)
from app.domain.evidence.source import TraceQuery

FIXTURES = (
    Path(__file__).parents[3] / "src" / "app" / "adapters" / "sources" / "fixtures" / "langsmith"
)


def _fixture_tree(name: str) -> dict[str, object]:
    with (FIXTURES / f"{name}.json").open(encoding="utf-8") as handle:
        payload = json.load(handle)
    return payload["trace"]


def _cohort_client(*names: str) -> FakeLangSmithClient:
    trees = [_fixture_tree(name) for name in names]
    roots = [{key: value for key, value in tree.items() if key != "child_runs"} for tree in trees]
    return FakeLangSmithClient(
        run_trees={str(tree["id"]): tree for tree in trees},
        query_results=roots,
    )


def _config() -> LangSmithSourceConfig:
    return LangSmithSourceConfig(
        api_key=SecretStr("sk-test-secret-123"),
        project="agent-reliability-lab",
    )


async def test_fetch_trace_maps_one_selected_run_tree() -> None:
    client = FakeLangSmithClient(
        run_trees={"run-1": _fixture_tree("phase2-01-bad-prompt-policy-answer")}
    )
    source = LangSmithSource(_config(), client=client)

    evidence = await source.fetch_trace("run-1")

    assert evidence.source.platform == "langsmith"
    assert evidence.source.trace_id == "20000000-0000-4000-8000-000000000011"
    assert evidence.reason_code == "policy_answer_ungrounded"
    assert client.fetch_calls == ["run-1"]
    serialized = str(evidence.model_dump(mode="json"))
    assert "sk-test-secret-123" not in serialized


async def test_deep_link_points_at_the_selected_run() -> None:
    url = langsmith_run_url(
        "https://smith.langchain.com",
        "agent-reliability-lab",
        "20000000-0000-4000-8000-000000000011",
        "10000000-0000-4000-8000-000000000011",
    )
    assert url == (
        "https://smith.langchain.com/projects/p/agent-reliability-lab/r/"
        "10000000-0000-4000-8000-000000000011?trace=20000000-0000-4000-8000-000000000011"
    )


@pytest.mark.parametrize(
    ("status_code", "error_type"),
    [
        (401, TraceAuthenticationError),
        (403, TraceAuthenticationError),
        (404, TraceNotFound),
        (429, TraceRateLimited),
        (500, TraceSourceUnavailable),
        (503, TraceSourceUnavailable),
    ],
)
async def test_http_failures_become_typed_errors(status_code: int, error_type: type) -> None:
    client = FakeLangSmithClient(
        error_factory=lambda: FakeLangSmithClient.status_error(status_code)
    )
    source = LangSmithSource(_config(), client=client)

    with pytest.raises(error_type):
        await source.fetch_trace("run-1")


async def test_network_failures_become_unavailable() -> None:
    client = FakeLangSmithClient(
        error_factory=lambda: httpx.ConnectError(
            "no route to host", request=httpx.Request("GET", "https://x")
        )
    )
    source = LangSmithSource(_config(), client=client)

    with pytest.raises(TraceSourceUnavailable):
        await source.fetch_trace("run-1")


async def test_timeout_failures_become_unavailable() -> None:
    client = FakeLangSmithClient(
        error_factory=lambda: httpx.TimeoutException(
            "timed out", request=httpx.Request("GET", "https://x")
        )
    )
    source = LangSmithSource(_config(), client=client)

    with pytest.raises(TraceSourceUnavailable):
        await source.fetch_trace("run-1")


async def test_typed_errors_never_contain_credentials() -> None:
    client = FakeLangSmithClient(error_factory=lambda: FakeLangSmithClient.status_error(401))
    source = LangSmithSource(_config(), client=client)

    try:
        await source.fetch_trace("run-1")
    except TraceAuthenticationError as error:
        assert "sk-test-secret-123" not in str(error)
        assert error.code == "trace_source_authentication_failed"
    else:
        pytest.fail("expected TraceAuthenticationError")


async def test_fetch_traces_returns_bounded_cohort() -> None:
    client = _cohort_client(
        "phase2-01-bad-prompt-policy-answer",
        "phase2-02-wrong-policy-evidence",
        "phase2-03-database-timeout",
    )
    source = LangSmithSource(_config(), client=client)

    cohort = await source.fetch_traces(TraceQuery(limit=2))

    assert len(cohort) == 2
    assert client.query_calls == [TraceQuery(limit=2)]
    assert len(client.fetch_calls) == 2


async def test_fetch_traces_filters_by_model_and_scenario() -> None:
    client = _cohort_client(
        "phase2-08-model-cost-comparison-primary",
        "phase2-08-model-cost-comparison-candidate",
    )
    source = LangSmithSource(_config(), client=client)

    cohort = await source.fetch_traces(TraceQuery(limit=5, model_name="gpt-4.1-mini"))

    assert [evidence.model.name for evidence in cohort] == ["gpt-4.1-mini"]  # type: ignore[union-attr]


async def test_fetch_traces_returns_empty_for_environment_filter() -> None:
    client = _cohort_client("phase2-01-bad-prompt-policy-answer")
    source = LangSmithSource(_config(), client=client)

    cohort = await source.fetch_traces(TraceQuery(limit=5, environment="production"))

    assert cohort == []
    assert client.query_calls == [TraceQuery(limit=5, environment="production")]


async def test_fetch_traces_skips_child_runs_and_picks_only_whole_traces() -> None:
    junk_root = {
        "id": "junk-run",
        "name": "probe.span",
        "parent_run_id": None,
        "start_time": "2026-08-06T10:00:00.000Z",
        "end_time": "2026-08-06T10:00:00.500Z",
        "run_type": "chain",
        "trace_id": "junk-trace",
        "error": None,
        "extra": {"metadata": {}},
    }
    child_of_root = {
        "id": "child-of-root",
        "name": "support_agent.routing",
        "parent_run_id": "root-1",
        "start_time": "2026-08-06T10:00:01.000Z",
        "end_time": "2026-08-06T10:00:01.500Z",
        "run_type": "chain",
        "trace_id": "root-trace",
        "error": None,
        "extra": {"metadata": {}},
    }
    valid_tree = _fixture_tree("phase2-01-bad-prompt-policy-answer")
    client = FakeLangSmithClient(
        query_results=[
            {key: value for key, value in valid_tree.items() if key != "child_runs"},
            child_of_root,
            junk_root,
        ],
        run_trees={
            str(valid_tree["id"]): valid_tree,
            "junk-run": {**junk_root, "child_runs": []},
        },
    )
    source = LangSmithSource(_config(), client=client)

    cohort = await source.fetch_traces(TraceQuery(limit=5))

    assert len(cohort) == 1
    assert cohort[0].reason_code == "policy_answer_ungrounded"
    assert "child-of-root" not in client.fetch_calls


async def test_fetch_traces_returns_empty_when_provider_has_no_runs() -> None:
    client = FakeLangSmithClient(query_results=[])
    source = LangSmithSource(_config(), client=client)

    assert await source.fetch_traces(TraceQuery(limit=5)) == []


async def test_single_trace_selection_stays_strict_about_junk() -> None:
    junk_tree = {
        "id": "junk-run",
        "name": "probe.span",
        "parent_run_id": None,
        "start_time": "2026-08-06T10:00:00.000Z",
        "end_time": "2026-08-06T10:00:00.500Z",
        "extra": {"metadata": {}},
        "child_runs": [],
    }
    client = FakeLangSmithClient(run_trees={"junk-run": junk_tree})
    source = LangSmithSource(_config(), client=client)

    with pytest.raises(UnsupportedTrace):
        await source.fetch_trace("junk-run")
