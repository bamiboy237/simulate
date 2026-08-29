"""This module defines HTTP routes for case runs, suite comparisons, and live events.

The routes are thin: they parse the request, delegate to the execution
service, and return stable typed results. Runs and comparisons execute in
background tasks; clients poll for the result and may consume the allowlisted
live event stream, which terminates cleanly.
"""

from typing import Annotated, AsyncIterator, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.api.dependencies import get_case_service, get_execution_service, get_suite_service
from app.domain.bundle.schemas import ConfigurationVersions
from app.domain.comparison.compare import RunComparison
from app.domain.comparison.experiment import ConfigurationChangeType
from app.domain.execution.service import ExecutionService
from app.domain.regression.schemas import RegressionCase
from app.domain.regression.service import RegressionCaseService
from app.domain.simulation.runner import SimulationRun
from app.domain.suite.schemas import SuiteComparisonResult
from app.domain.suite.service import SuiteService

runs_router = APIRouter(prefix="/runs", tags=["runs"])
comparisons_router = APIRouter(prefix="/comparisons", tags=["comparisons"])


class StartRunRequest(BaseModel):
    """This class stores the body of one run start request."""

    case_id: UUID
    case_version: int = Field(ge=1)


class StartComparisonRequest(BaseModel):
    """This class stores the body of one suite comparison start request."""

    suite_id: UUID
    suite_version: int = Field(ge=1)
    change_type: ConfigurationChangeType
    candidate: ConfigurationVersions
    candidate_prompt: str | None = Field(default=None, max_length=2000)
    candidate_prompt_version: str | None = Field(default=None, max_length=50)


class ExecutionStarted(BaseModel):
    """This class stores the stable handle of one started execution."""

    execution_id: UUID
    kind: Literal["run", "comparison"]
    status: str


class ExecutionStatus(BaseModel):
    """This class stores the polled status of one execution."""

    execution_id: UUID
    status: Literal["running", "completed", "failed"]
    result: SimulationRun | RunComparison | SuiteComparisonResult | None = None
    error_code: str | None = None
    error_message: str | None = None





@runs_router.post("", response_model=ExecutionStarted, status_code=status.HTTP_202_ACCEPTED)
async def start_run(
    request: StartRunRequest,
    cases: Annotated[RegressionCaseService, Depends(get_case_service)],
    execution: Annotated[ExecutionService, Depends(get_execution_service)],
) -> ExecutionStarted:
    """This method starts one saved case run in a background task."""
    case = await cases.get_case(case_id=request.case_id, case_version=request.case_version)
    handle = execution.start_run(case=case)
    return ExecutionStarted(
        execution_id=handle.execution_id,
        kind="run",
        status=handle.status,
    )


@runs_router.get(
    "/{execution_id}",
    response_model=ExecutionStatus,
    operation_id="run_status_runs__execution_id__get",
)
@comparisons_router.get(
    "/{execution_id}",
    response_model=ExecutionStatus,
    operation_id="comparison_status_comparisons__execution_id__get",
)
async def execution_status(
    execution_id: UUID,
    execution: Annotated[ExecutionService, Depends(get_execution_service)],
) -> ExecutionStatus:
    """This method returns one execution's status and its result when completed."""
    handle = execution.require(execution_id)
    return ExecutionStatus(
        execution_id=execution_id,
        status=handle.status,  # type: ignore[arg-type]
        result=handle.result,  # type: ignore[arg-type]
        error_code=handle.error_code,
        error_message=handle.error_message,
    )


@runs_router.get("/{execution_id}/events")
async def run_events(
    execution_id: UUID,
    execution: Annotated[ExecutionService, Depends(get_execution_service)],
) -> StreamingResponse:
    """This method streams one run's allowlisted live events until completion."""
    execution.require(execution_id)

    async def generate() -> AsyncIterator[str]:
        async for event in execution.events(execution_id):
            yield f"data: {event.model_dump_json()}\n\n"
        yield 'data: {"done": true}\n\n'

    return StreamingResponse(generate(), media_type="text/event-stream")


@comparisons_router.post(
    "", response_model=ExecutionStarted, status_code=status.HTTP_202_ACCEPTED
)
async def start_comparison(
    request: StartComparisonRequest,
    suites: Annotated[SuiteService, Depends(get_suite_service)],
    cases: Annotated[RegressionCaseService, Depends(get_case_service)],
    execution: Annotated[ExecutionService, Depends(get_execution_service)],
) -> ExecutionStarted:
    """This method starts one suite comparison in a background task."""
    suite = await suites.get_suite(
        suite_id=request.suite_id, suite_version=request.suite_version
    )
    resolved: list[RegressionCase] = []
    for member in suite.members:
        resolved.append(
            await cases.get_case(case_id=member.case_id, case_version=member.case_version)
        )
    handle = execution.start_comparison(
        suite=suite,
        cases=resolved,
        change_type=request.change_type,
        candidate=request.candidate,
        candidate_prompt=request.candidate_prompt,
        candidate_prompt_version=request.candidate_prompt_version,
    )
    return ExecutionStarted(
        execution_id=handle.execution_id,
        kind="comparison",
        status=handle.status,
    )
