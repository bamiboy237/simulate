"""Focused HTTP tests for the support-experiment operator delivery surface."""

from datetime import UTC, datetime
from uuid import UUID, uuid4

from fastapi.testclient import TestClient
from pydantic import SecretStr

from app.api.dependencies import get_support_experiment_operator
from app.config import Settings
from app.domain.experiment.models import ExperimentRecord
from app.main import create_app

NOW = datetime(2026, 9, 4, tzinfo=UTC)
OPERATOR_TOKEN = "test-experiment-operator-token"


class RecordingOperator:
    """A boundary double that records route inputs without emulating domain behavior."""

    def __init__(self) -> None:
        self.record = ExperimentRecord(
            id=uuid4(),
            contract={},
            contract_hash="a" * 64,
            status="pending",
            created_at=NOW,
        )

    async def cancel(self, experiment_id: UUID) -> ExperimentRecord:
        assert experiment_id == self.record.id
        self.record.status = "cancelled"
        self.record.finished_at = NOW
        return self.record


def _client(operator: RecordingOperator, *, configured: bool = True) -> TestClient:
    app = create_app(
        Settings(
            database_url="postgresql://user:password@localhost:5432/app",
            environment="test",
            experiment_operator_token=SecretStr(OPERATOR_TOKEN) if configured else None,
            _env_file=None,
        )
    )
    app.dependency_overrides[get_support_experiment_operator] = lambda: operator
    return TestClient(app)


def _operator_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {OPERATOR_TOKEN}"}


def test_experiment_cancel_route_delegates_pending_cancellation() -> None:
    operator = RecordingOperator()

    with _client(operator) as client:
        response = client.post(
            f"/experiments/{operator.record.id}/cancel",
            headers=_operator_headers(),
        )

    assert response.status_code == 200
    assert response.json()["status"] == "cancelled"


def test_experiment_routes_reject_missing_or_unconfigured_operator_credentials() -> None:
    operator = RecordingOperator()

    with _client(operator) as client:
        unauthorized = client.get(f"/experiments/{operator.record.id}")

    assert unauthorized.status_code == 401
    assert unauthorized.json()["error"]["code"] == "experiment_operator_unauthorized"

    with _client(operator, configured=False) as client:
        unavailable = client.get(f"/experiments/{operator.record.id}")

    assert unavailable.status_code == 503
    assert unavailable.json()["error"]["code"] == "experiment_operator_not_configured"
