"""This module builds the Phase 7 application services for HTTP routes."""

from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.pydantic_ai_agent import ModelConfig
from app.config import Settings, get_settings
from app.db import get_session, get_session_factory
from app.domain.agent.errors import ModelNotConfigured
from app.domain.execution.errors import SandboxUnavailableError
from app.domain.execution.service import ExecutionService
from app.domain.failures.repository import SqlAlchemyFailureReviewRepository
from app.domain.failures.service import FailureReviewService
from app.domain.investigation.repository import SqlAlchemyInvestigationRepository
from app.domain.investigation.service import InvestigationService
from app.domain.regression.repository import SqlAlchemyRegressionCaseRepository
from app.domain.regression.service import RegressionCaseService
from app.domain.simulation.provisioner import postgres_provisioner_factory
from app.domain.suite.repository import SqlAlchemySuiteRepository
from app.domain.suite.service import SuiteService


def get_case_service(
    session: Annotated[AsyncSession, Depends(get_session)],
) -> RegressionCaseService:
    """This function builds the case service from one database session."""
    return RegressionCaseService(SqlAlchemyRegressionCaseRepository(session))


def get_suite_service(
    session: Annotated[AsyncSession, Depends(get_session)],
) -> SuiteService:
    """This function builds the suite service from one database session."""
    return SuiteService(
        SqlAlchemySuiteRepository(session),
        SqlAlchemyRegressionCaseRepository(session),
    )


def get_failure_review_service(
    session: Annotated[AsyncSession, Depends(get_session)],
) -> FailureReviewService:
    """Build the failure proposal review service from one database session."""
    return FailureReviewService(SqlAlchemyFailureReviewRepository(session))


def _build_execution_service(settings: Settings) -> ExecutionService:
    """This function builds the execution service from the deployed settings.

    Simulation runs require an isolated test environment; the sandbox target
    rejects any other environment with a safe typed error.
    """
    if not settings.model_configured:
        raise ModelNotConfigured()
    provider = settings.model_provider
    model_name = settings.model_name
    if provider is None or model_name is None:
        raise ModelNotConfigured()
    try:
        provisioner_factory = postgres_provisioner_factory(
            get_session_factory(),
            database_url=str(settings.migration_database_url),
            environment=settings.environment,
            isolation_confirmed=settings.environment == "test",
        )
    except ValueError as error:
        raise SandboxUnavailableError(detail=str(error)) from error
    return ExecutionService(
        provisioner_factory=provisioner_factory,
        model_config=ModelConfig(
            provider=provider,
            name=model_name,
            base_url=settings.model_base_url,
            api_key=settings.model_api_key,
        ),
    )


def get_execution_service(
    request: Request,
    settings: Annotated[Settings, Depends(get_settings)],
) -> ExecutionService:
    """This function returns the app-scoped execution service.

    The service owns the registry of background executions, so one service
    instance lives on the application and every request sees the same runs.
    """
    service = getattr(request.app.state, "execution_service", None)
    if service is None:
        service = _build_execution_service(settings)
        request.app.state.execution_service = service
    return service


def get_investigation_service(
    session: Annotated[AsyncSession, Depends(get_session)],
) -> InvestigationService:
    """Build the investigation lifecycle and persistence service from a database session."""
    return InvestigationService(SqlAlchemyInvestigationRepository(session))
