"""Authenticated HTTP delivery for the synchronous support-experiment operator."""

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Query, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, ConfigDict, SecretStr

from app.api.dependencies import get_support_experiment_operator
from app.config import Settings, get_settings
from app.domain.experiment.events import ExperimentEvent
from app.domain.experiment.models import ExperimentRecord
from app.domain.experiment.operator import (
    SupportExperimentDraft,
    SupportExperimentOperator,
    SupportExperimentStartInputs,
    SupportSuiteExperimentDraft,
    authorize_support_experiment_operator,
)
from app.domain.experiment.support_results import (
    SupportExperimentResult,
    SupportResult,
    SupportSuiteExperimentResult,
)

_operator_bearer = HTTPBearer(auto_error=False)


def require_experiment_operator_credential(
    credentials: Annotated[
        HTTPAuthorizationCredentials | None,
        Depends(_operator_bearer),
    ],
    settings: Annotated[Settings, Depends(get_settings)],
) -> SecretStr:
    """Require the one configured operator credential for every experiment route."""
    credential = SecretStr(credentials.credentials) if credentials is not None else None
    return authorize_support_experiment_operator(settings, credential)


router = APIRouter(
    prefix="/experiments",
    tags=["experiments"],
    dependencies=[Depends(require_experiment_operator_credential)],
)


class ExperimentStatusResponse(BaseModel):
    """The safe status view of a durable experiment record."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    experiment_id: UUID
    status: str
    execution_id: UUID | None
    contract_hash: str
    error_code: str | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None


class SynchronousStartResponse(ExperimentStatusResponse):
    """The terminal result of one synchronous start request."""

    execution_mode: Literal["synchronous_request"] = "synchronous_request"


def _status_view(record: ExperimentRecord) -> ExperimentStatusResponse:
    return ExperimentStatusResponse(
        experiment_id=record.id,
        status=record.status,
        execution_id=record.execution_id,
        contract_hash=record.contract_hash,
        error_code=record.error_code,
        created_at=record.created_at,
        started_at=record.started_at,
        finished_at=record.finished_at,
    )


@router.post("", response_model=ExperimentStatusResponse, status_code=status.HTTP_201_CREATED)
async def create_experiment(
    draft: SupportExperimentDraft | SupportSuiteExperimentDraft,
    operator: Annotated[
        SupportExperimentOperator,
        Depends(get_support_experiment_operator),
    ],
) -> ExperimentStatusResponse:
    """Create a pending experiment from one approved case or exact saved suite."""
    return _status_view(await operator.create(draft))


@router.post(
    "/{experiment_id}/start",
    response_model=SynchronousStartResponse,
)
async def start_experiment(
    experiment_id: UUID,
    inputs: SupportExperimentStartInputs,
    operator_credential: Annotated[
        SecretStr,
        Depends(require_experiment_operator_credential),
    ],
    operator: Annotated[
        SupportExperimentOperator,
        Depends(get_support_experiment_operator),
    ],
) -> SynchronousStartResponse:
    """Run all repetitions in this request before returning its terminal record."""
    record = await operator.start(
        experiment_id,
        inputs,
        operator_credential=operator_credential,
    )
    return SynchronousStartResponse(**_status_view(record).model_dump())


@router.get("/{experiment_id}", response_model=ExperimentStatusResponse)
async def experiment_status(
    experiment_id: UUID,
    operator: Annotated[
        SupportExperimentOperator,
        Depends(get_support_experiment_operator),
    ],
) -> ExperimentStatusResponse:
    """Return durable lifecycle status without exposing prompt bodies."""
    return _status_view(await operator.get_record(experiment_id))


@router.get("/{experiment_id}/events", response_model=list[ExperimentEvent])
async def replay_experiment_events(
    experiment_id: UUID,
    operator: Annotated[
        SupportExperimentOperator,
        Depends(get_support_experiment_operator),
    ],
    after_sequence: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=1_000)] = 100,
) -> list[ExperimentEvent]:
    """Replay ordered, persisted experiment events after an explicit cursor."""
    return await operator.replay(
        experiment_id,
        after_sequence=after_sequence,
        limit=limit,
    )


@router.post("/{experiment_id}/cancel", response_model=ExperimentStatusResponse)
async def cancel_experiment(
    experiment_id: UUID,
    operator: Annotated[
        SupportExperimentOperator,
        Depends(get_support_experiment_operator),
    ],
) -> ExperimentStatusResponse:
    """Cancel a pending experiment."""
    return _status_view(await operator.cancel(experiment_id))


@router.get(
    "/{experiment_id}/result",
    response_model=SupportExperimentResult | SupportSuiteExperimentResult,
)
async def export_verified_result(
    experiment_id: UUID,
    operator: Annotated[
        SupportExperimentOperator,
        Depends(get_support_experiment_operator),
    ],
) -> SupportResult:
    """Export only a result that passes durable contract and hash verification."""
    return await operator.verified_result(experiment_id)
