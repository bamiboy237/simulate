"""Live event-order and rollback integration for the persona simulator.

Runs the real support sandbox against the isolated PostgreSQL database with
deterministic fake model boundaries, then proves the engine streams events in
order, never persists chat text, and rolls the transaction back.
"""

from __future__ import annotations

from types import SimpleNamespace
from uuid import UUID

import pytest
from alembic import command
from alembic.config import Config
from pydantic import ValidationError
from sqlalchemy import func, select

from app.config import Settings
from app.db import get_session_factory
from app.domain.agent.schemas import (
    AnswerContext,
    ReasonCode,
    RouteIntent,
    RoutingDecision,
    SupportOutcome,
    SupportRequest,
    SupportResponse,
)
from app.domain.bundle.extract import synthetic_id
from app.domain.simulation.scenarios import SCENARIO_BY_ID
from app.domain.support.models import Order, Ticket
from app.domain.support.schemas import OrderStatus
from app.domain.user_simulator import simulator
from app.domain.user_simulator.events import EventKind, SimulationEvent
from app.domain.user_simulator.models import UserTurn
from app.domain.user_simulator.personas import SUPPORT_PERSONAS


@pytest.fixture(scope="module", autouse=True)
def apply_migrations() -> None:
    try:
        Settings()  # type: ignore[call-arg]
    except ValidationError:
        pytest.skip("DATABASE_URL is required for simulation integration tests")
    command.upgrade(Config("alembic.ini"), "head")


async def _snapshot() -> tuple[list[tuple[object, object]], int]:
    async with get_session_factory()() as session:
        orders = (
            await session.execute(select(Order.id, Order.status).order_by(Order.id))
        ).all()
        tickets = await session.scalar(select(func.count()).select_from(Ticket))
    return list(orders), tickets or 0


class _PersonaAgent:
    """Deterministic fake for the hosted persona model boundary."""

    def __init__(self, *args: object, **kwargs: object) -> None:
        pass

    async def run(self, prompt: str, **kwargs: object) -> SimpleNamespace:
        del prompt, kwargs
        return SimpleNamespace(
            output=UserTurn(message="Please refund my order"),
            usage=SimpleNamespace(total_tokens=3, cost=None),
        )


class _FakeSupportAgent:
    """Fake for the product agent; uses the real recorder and real repository.

    The recorder spans make the engine stream MODEL/TOOL events exactly like
    the real agent, and the repository write hits the disposable sandbox so
    the rollback assertion has real state to roll back.
    """

    order_id: UUID | None = None

    def __init__(
        self, *, model_config: object, recorder: object, repository: object, **kwargs: object
    ) -> None:
        del model_config, kwargs
        self._recorder = recorder
        self._repository = repository

    async def handle(self, request: SupportRequest) -> SupportResponse:
        del request
        with self._recorder.span("support_agent.routing") as span:  # type: ignore[attr-defined]
            span.set_attribute("model.tokens.total", 5)
            span.set_attribute("model.latency.ms", 8.0)
        with self._recorder.span("support_agent.answer") as span:  # type: ignore[attr-defined]
            span.set_attribute("model.tokens.total", 4)
            span.set_attribute("model.latency.ms", 9.0)
        assert self.order_id is not None
        order = await self._repository.get_order(self.order_id)  # type: ignore[attr-defined]
        assert order is not None
        with self._recorder.span("support_agent.tool.confirm_refund") as span:  # type: ignore[attr-defined]
            span.set_attribute("tool.name", "confirm_refund")
            span.set_attribute("tool.order.id", str(order.id))
            saved = await self._repository.save_order(  # type: ignore[attr-defined]
                order.model_copy(update={"status": OrderStatus.REFUNDED})
            )
        assert saved is not None
        return SupportResponse(
            intent=RouteIntent.REFUND,
            outcome=SupportOutcome.COMPLETED,
            reason_code=ReasonCode.REFUND_CONFIRMED,
            message="Your refund was confirmed.",
            context=AnswerContext(
                routing=RoutingDecision(intent=RouteIntent.REFUND, confidence=1.0)
            ),
        )


class _CaptureSink:
    def __init__(self) -> None:
        self.events: list[SimulationEvent] = []

    def emit(self, event: SimulationEvent) -> None:
        self.events.append(event)


@pytest.mark.integration
async def test_support_run_streams_events_in_order_and_rolls_back(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    before = await _snapshot()
    persona = SUPPORT_PERSONAS[4]  # phase2-05-unconfirmed-refund
    scenario = SCENARIO_BY_ID[persona.scenario_or_workflow_id]
    # The bundle compiler seeds synthetic order ids, so the agent must use
    # the same synthetic identity the sandbox seeded.
    _FakeSupportAgent.order_id = synthetic_id(scenario.initial_state.orders[0].id)

    monkeypatch.setattr(simulator, "Agent", _PersonaAgent)
    monkeypatch.setattr(simulator, "live_model", lambda: object())
    monkeypatch.setattr(simulator, "require_live_test_environment", lambda: None)
    monkeypatch.setattr(
        "app.adapters.pydantic_ai_agent.PydanticAISupportAgent", _FakeSupportAgent
    )

    capture = _CaptureSink()
    result = await simulator.run_support(
        persona, max_turns=2, root=tmp_path, event_sink=capture
    )

    kinds = [event.display.kind for event in capture.events if event.display is not None]
    assert kinds[0] is EventKind.START
    # persona chat, then support model/tool events, then the final boundaries
    assert EventKind.USER in kinds
    assert EventKind.AGENT in kinds
    assert kinds.index(EventKind.TOOL_SELECTED) < kinds.index(EventKind.TOOL_RESULT)
    assert EventKind.MODEL in kinds
    assert EventKind.STATE in kinds
    assert kinds[-2] is EventKind.CLEANUP
    assert kinds[-1] is EventKind.DONE
    assert EventKind.ERROR not in kinds

    # chat text never reaches the persistent JSONL log
    persistent = (tmp_path / f"{result.report.run_id}.jsonl").read_text()
    assert "Please refund my order" not in persistent
    assert "Your refund was confirmed." not in persistent

    # the disposable transaction was rolled back: the real DB is unchanged
    assert await _snapshot() == before


@pytest.mark.integration
async def test_injected_profile_url_reaches_provisioner_not_root_database_url(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """A conflicting remote root DATABASE_URL must never reach the sandbox.

    The selected profile's loopback URL is injected into ``run_support``; the
    provisioner spy proves that exact URL is used, and the run completes
    against the local sandbox even though the root settings would point at an
    unreachable remote host.
    """
    from app.config import Settings
    from app.domain.simulation import provisioner

    local = str(Settings().migration_database_url)  # type: ignore[call-arg]
    remote = "postgresql://root:secret@db.example.invalid:5432/prod"
    monkeypatch.setenv("DATABASE_URL", remote)
    monkeypatch.setenv("DATABASE_URL_UNPOOLED", remote)
    # The engine's own settings loader (imported inside run_support) must see
    # the conflicting remote root URL; the injected profile URL still wins.
    monkeypatch.setattr("app.config.get_settings", lambda: Settings())  # type: ignore[arg-type]
    captured: dict[str, object] = {}
    real_factory = provisioner.postgres_provisioner_factory

    def spy_factory(
        session_factory: object,
        *,
        database_url: str,
        environment: str,
        isolation_confirmed: bool,
    ) -> object:
        captured["database_url"] = database_url
        return real_factory(  # type: ignore[no-any-return]
            session_factory,  # type: ignore[arg-type]
            database_url=database_url,
            environment=environment,
            isolation_confirmed=isolation_confirmed,
        )

    monkeypatch.setattr(
        "app.domain.simulation.provisioner.postgres_provisioner_factory", spy_factory
    )
    monkeypatch.setattr(simulator, "Agent", _PersonaAgent)
    monkeypatch.setattr(simulator, "live_model", lambda: object())
    monkeypatch.setattr(simulator, "require_live_test_environment", lambda: None)
    monkeypatch.setattr(
        "app.adapters.pydantic_ai_agent.PydanticAISupportAgent", _FakeSupportAgent
    )

    persona = SUPPORT_PERSONAS[4]
    scenario = SCENARIO_BY_ID[persona.scenario_or_workflow_id]
    _FakeSupportAgent.order_id = synthetic_id(scenario.initial_state.orders[0].id)
    result = await simulator.run_support(
        persona, max_turns=1, root=tmp_path, database_url=local
    )
    assert captured["database_url"] == local
    assert result.report.run_id  # completed against the local loopback sandbox
