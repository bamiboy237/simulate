"""This module provides the LangSmith trace source adapter.

The adapter fetches one selected run tree or an explicit bounded cohort.
The adapter maps LangSmith run records into vendor-neutral TraceEvidence.
Authentication, rate limits, and provider failures become typed errors.
"""

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, NoReturn, Protocol
from urllib.parse import quote

import httpx
from pydantic import SecretStr

from app.domain.evidence.errors import (
    InvalidEvidence,
    TraceAuthenticationError,
    TraceNotFound,
    TraceRateLimited,
    TraceSourceError,
    TraceSourceUnavailable,
    UnsupportedTrace,
)
from app.domain.evidence.mapping import SourceRun, normalize_runs
from app.domain.evidence.schemas import TraceEvidence
from app.domain.evidence.source import TraceQuery, evidence_matches_query
from app.telemetry.allowlist import TRACE_ATTRIBUTE_ALLOWLIST

LANGSMITH_API_BASE_URL = "https://api.smith.langchain.com"
LANGSMITH_RUN_BASE_URL = "https://smith.langchain.com"


@dataclass(frozen=True)
class LangSmithSourceConfig:
    """This class stores credentials and endpoints for one LangSmith source."""

    api_key: SecretStr
    project: str
    api_base_url: str = LANGSMITH_API_BASE_URL
    run_base_url: str = LANGSMITH_RUN_BASE_URL
    timeout_seconds: float = 15.0
    scenario_id: str | None = None


def langsmith_run_url(run_base_url: str, project: str, trace_id: str, run_id: str) -> str:
    """This function builds the deep link to one LangSmith run."""
    safe_project = quote(project, safe="")
    return f"{run_base_url}/projects/p/{safe_project}/r/{run_id}?trace={trace_id}"


def translate_client_error(error: Exception) -> NoReturn:
    """This function maps provider failures into safe typed errors."""
    if isinstance(error, TraceSourceError):
        raise error
    if isinstance(error, httpx.HTTPStatusError):
        status_code = error.response.status_code
        if status_code in (401, 403):
            raise TraceAuthenticationError() from error
        if status_code == 404:
            raise TraceNotFound() from error
        if status_code == 429:
            raise TraceRateLimited() from error
        raise TraceSourceUnavailable() from error
    if isinstance(error, (httpx.TimeoutException, httpx.RequestError)):
        raise TraceSourceUnavailable() from error
    raise TraceSourceUnavailable() from error


def _json_response(response: httpx.Response) -> object:
    if response.is_error:
        translate_client_error(
            httpx.HTTPStatusError(
                f"request failed with status {response.status_code}",
                request=response.request,
                response=response,
            )
        )
    try:
        return response.json()
    except json.JSONDecodeError as error:
        raise TraceSourceUnavailable() from error


class RunClient(Protocol):
    """This protocol defines the network operations the source adapter needs."""

    async def fetch_run_tree(self, run_id: str) -> dict[str, Any]: ...

    async def query_root_runs(self, query: TraceQuery) -> list[dict[str, Any]]: ...

    async def close(self) -> None: ...


class LangSmithClient:
    """This class talks to the LangSmith API and raises provider failures raw.

    The source adapter translates these failures into typed errors.
    This class implements the shapes verified against the live API:
    projects resolve through GET /sessions; runs come from POST /runs/query.
    """

    def __init__(self, config: LangSmithSourceConfig) -> None:
        self._config = config
        self._http = httpx.AsyncClient(
            base_url=config.api_base_url,
            headers={"x-api-key": config.api_key.get_secret_value()},
            timeout=config.timeout_seconds,
        )
        self._session_id: str | None = None

    async def _query_runs(self, payload: dict[str, Any]) -> list[dict[str, Any]]:
        """This method posts one run query and unwraps the run list."""
        response = await self._http.post("/runs/query", json=payload)
        body = _json_response(response)
        if not isinstance(body, dict) or not isinstance(body.get("runs"), list):
            raise TraceSourceUnavailable()
        return [run for run in body["runs"] if isinstance(run, dict)]

    async def _resolve_session_id(self) -> str:
        """This method resolves the configured project name to its session id."""
        if self._session_id is not None:
            return self._session_id
        response = await self._http.get(
            "/sessions",
            params={"name": self._config.project, "limit": 1},
        )
        body = _json_response(response)
        if not isinstance(body, list):
            raise TraceSourceUnavailable()
        for session in body:
            if isinstance(session, dict) and isinstance(session.get("id"), str):
                self._session_id = session["id"]
                return self._session_id
        raise TraceNotFound()

    async def fetch_run_tree(self, run_id: str) -> dict[str, Any]:
        """This method fetches every run in the selected trace."""
        selected = await self._query_runs({"id": [run_id], "limit": 1})
        if not selected:
            raise TraceNotFound()
        trace_id = selected[0].get("trace_id") or selected[0].get("id")
        if not isinstance(trace_id, str):
            raise TraceSourceUnavailable()
        trace_runs = await self._query_runs({"trace": trace_id})
        for tree in trees_from_flat_runs(trace_runs):
            if any(run.get("id") == run_id for run in flatten_run_tree(tree)):
                return tree
        raise TraceSourceUnavailable()

    async def query_root_runs(self, query: TraceQuery) -> list[dict[str, Any]]:
        """This method fetches runs for one bounded cohort query.

        The API returns child runs in the same response, so this method
        requests a window of ``limit * 10`` (capped at 100) and the source
        keeps only whole traces.
        """
        session_id = await self._resolve_session_id()
        payload: dict[str, Any] = {
            "session": [session_id],
            "limit": min(query.limit * 10, 100),
            "is_root": True,
        }
        if query.since is not None:
            payload["start_time"] = query.since.isoformat()
        if query.until is not None:
            payload["end_time"] = query.until.isoformat()
        return await self._query_runs(payload)

    async def close(self) -> None:
        await self._http.aclose()


def flatten_run_tree(run: Mapping[str, Any]) -> list[dict[str, Any]]:
    """This function flattens one run tree into pre-order (parents first)."""
    flattened: list[dict[str, Any]] = []
    pending = [run]
    while pending:
        current = pending.pop(0)
        flattened.append(dict(current))
        children = current.get("child_runs") or ()
        pending[0:0] = [dict(child) for child in children if isinstance(child, dict)]
    return flattened


def trees_from_flat_runs(runs: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """This function groups one flat run list into whole run trees.

    The live API returns child runs in the same cohort response, so trees
    are assembled locally without extra requests. Runs whose parent is
    missing from the response are fragments, not whole traces; they are
    dropped instead of becoming broken roots.
    """
    by_id: dict[str, dict[str, Any]] = {
        str(run["id"]): dict(run) for run in runs if isinstance(run.get("id"), str)
    }
    for run in by_id.values():
        run["child_runs"] = []
    roots: list[dict[str, Any]] = []
    for run in by_id.values():
        parent_id = run.get("parent_run_id")
        if isinstance(parent_id, str) and parent_id in by_id:
            by_id[parent_id]["child_runs"].append(run)
        elif parent_id is None:
            roots.append(run)
    return roots


def _metadata_of(run: Mapping[str, Any]) -> dict[str, object]:
    """This function collects run metadata and translates LangSmith prefixes.

    ``langsmith.metadata.*`` attributes arrive as plain metadata keys.
    OTel resource attributes arrive as ``otel.resource.*`` keys; the prefix
    is stripped when the remaining name is allowlisted evidence.
    """
    metadata: dict[str, object] = {}
    top_level = run.get("metadata")
    if isinstance(top_level, dict):
        metadata.update(top_level)
    extra = run.get("extra")
    if isinstance(extra, dict):
        nested = extra.get("metadata")
        if isinstance(nested, dict):
            metadata.update(nested)
    translated: dict[str, object] = {}
    for key, value in metadata.items():
        if key.startswith("langsmith.metadata."):
            translated[key[len("langsmith.metadata.") :]] = value
        elif key.startswith("otel.resource."):
            stripped = key[len("otel.resource.") :]
            translated[stripped if stripped in TRACE_ATTRIBUTE_ALLOWLIST else key] = value
        else:
            translated[key] = value
    return translated


def source_run_from_langsmith(run: Mapping[str, Any]) -> SourceRun:
    """This function converts one LangSmith run into a neutral source run."""
    run_id = run.get("id")
    if not isinstance(run_id, str) or not run_id:
        raise TraceSourceUnavailable()
    name = run.get("name")
    if not isinstance(name, str) or not name:
        raise TraceSourceUnavailable()
    parent_id = run.get("parent_run_id")
    start_value = run.get("start_time")
    if not isinstance(start_value, str):
        raise TraceSourceUnavailable()
    try:
        start_time = datetime.fromisoformat(start_value.replace("Z", "+00:00"))
    except ValueError as error:
        raise TraceSourceUnavailable() from error
    end_value = run.get("end_time")
    end_time: datetime | None = None
    if isinstance(end_value, str):
        try:
            end_time = datetime.fromisoformat(end_value.replace("Z", "+00:00"))
        except ValueError as error:
            raise TraceSourceUnavailable() from error
    trace_id = run.get("trace_id")
    raw_error = run.get("error")
    return SourceRun(
        run_id=run_id,
        name=name,
        parent_id=str(parent_id) if isinstance(parent_id, str) and parent_id else None,
        start_time=start_time,
        end_time=end_time,
        attributes=_metadata_of(run),
        error=str(raw_error) if isinstance(raw_error, str) and raw_error else None,
        trace_id=str(trace_id) if isinstance(trace_id, str) and trace_id else None,
    )


class LangSmithSource:
    """This class provides the TraceSource contract for LangSmith."""

    def __init__(
        self,
        config: LangSmithSourceConfig,
        client: RunClient | None = None,
    ) -> None:
        self._config = config
        self._client = client or LangSmithClient(config)

    def _evidence_for_tree(self, tree: Mapping[str, Any]) -> TraceEvidence:
        runs = [source_run_from_langsmith(run) for run in flatten_run_tree(tree)]
        root = next((run for run in runs if run.parent_id is None), None)
        if root is None:
            raise TraceSourceUnavailable()
        trace_id = root.trace_id or root.run_id
        source_url = langsmith_run_url(
            self._config.run_base_url,
            self._config.project,
            trace_id,
            root.run_id,
        )
        return normalize_runs(
            runs,
            platform="langsmith",
            project=self._config.project,
            scenario_id=self._config.scenario_id,
            source_url=source_url,
        )

    async def fetch_trace(self, source_trace_id: str) -> TraceEvidence:
        """This method fetches one selected trace and maps it to evidence."""
        try:
            tree = await self._client.fetch_run_tree(source_trace_id)
        except Exception as error:
            translate_client_error(error)
        return self._evidence_for_tree(tree)

    async def fetch_traces(self, query: TraceQuery) -> list[TraceEvidence]:
        """This method fetches one explicit bounded cohort and maps it to evidence.

        The first query selects a bounded set of roots. Each selected root is
        then fetched as a complete trace. Runs that do not map to the evidence
        contract are skipped. Single-trace selection stays strict.
        """
        try:
            roots = await self._client.query_root_runs(query)
            evidence_list: list[TraceEvidence] = []
            for root in roots:
                if root.get("parent_run_id") is not None:
                    continue
                root_id = root.get("id")
                if not isinstance(root_id, str):
                    continue
                tree = await self._client.fetch_run_tree(root_id)
                try:
                    evidence = self._evidence_for_tree(tree)
                except (InvalidEvidence, UnsupportedTrace):
                    continue
                if evidence_matches_query(evidence, query):
                    evidence_list.append(evidence)
                    if len(evidence_list) >= query.limit:
                        break
        except Exception as error:
            translate_client_error(error)
        return evidence_list

    async def close(self) -> None:
        await self._client.close()
