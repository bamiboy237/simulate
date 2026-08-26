"""Hosted persona/user simulator with state-verified termination.

This module intentionally has no offline model path.  It is a live test tool and
requires the explicit test environment and the reviewed Luna model.

The engine emits a generic event stream (see ``events.py``) through an optional
external sink: persona turns, support span/tool/state events, reference
tool/approval/retry/state events, and cleanup/final boundaries.  Conversation
text is display-only memory and is never persisted.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast
from uuid import UUID, uuid4

from pydantic_ai import Agent, UsageLimits
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.pydantic_ai_agent import ModelConfig, build_pydantic_ai_model
from app.domain.user_simulator.events import (
    DisplayMemory,
    EventEmitter,
    EventKind,
    EventSink,
    EventSource,
    JsonlPersistentSink,
    NonFatalSink,
)
from app.domain.user_simulator.flows import ToolProjector
from app.domain.user_simulator.models import BusinessChoice, SimulatorReport, UserTurn
from app.domain.user_simulator.personas import PersonaDefinition

MODEL_NAME = "gpt-5.6-luna"
MODEL_PROVIDER = "openai"
DEFAULT_MAX_TURNS = 12
_UUID_PATTERN = re.compile(
    r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[1-5][0-9a-fA-F]{3}-"
    r"[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}\b"
)


@dataclass
class _UsageTotals:
    """Accumulate Pydantic AI usage without inventing provider costs."""

    total_tokens: int = 0
    _cost_total: float = 0.0
    _cost_known: bool = True
    _saw_usage: bool = False

    def add(self, usage: object | None) -> None:
        """Add one result usage object, treating missing cost as unknown."""
        if usage is None:
            return
        self._saw_usage = True
        self.total_tokens += int(getattr(usage, "total_tokens", 0) or 0)
        cost = getattr(usage, "cost", None)
        if cost is None:
            self._cost_known = False
        elif self._cost_known:
            self._cost_total += float(cost)

    def add_model_span(self, name: str, attributes: Mapping[str, object], *, ended: bool) -> None:
        """Add final model span usage, once, from sanitized support attributes."""
        if not ended or name not in {"support_agent.routing", "support_agent.answer"}:
            return
        total = attributes.get("model.tokens.total")
        if not isinstance(total, (int, float)) or isinstance(total, bool):
            return
        self._saw_usage = True
        self.total_tokens += int(total)
        cost = attributes.get("model.cost.usd")
        if not isinstance(cost, (int, float)) or isinstance(cost, bool):
            self._cost_known = False
        elif self._cost_known:
            self._cost_total += float(cost)

    @property
    def cost_usd(self) -> float | None:
        """Return a cost only when every observed call supplied one."""
        if not self._saw_usage or not self._cost_known:
            return None
        return self._cost_total


def require_live_test_environment() -> None:
    """Verify that a model API key is configured."""
    from app.config import get_settings

    settings = get_settings()
    if not settings.model_configured:
        raise RuntimeError("user simulator requires a configured model API key")


def live_model() -> object:
    require_live_test_environment()
    from app.config import get_settings

    settings = get_settings()
    return build_pydantic_ai_model(
        ModelConfig(
            provider="openai",
            name=MODEL_NAME,
            base_url=settings.model_base_url,
            api_key=settings.model_api_key,
        )
    )


def _safe_failure_reason(error: Exception) -> str:
    """Return an allowlisted failure class without retaining provider response bodies."""
    status_code = getattr(error, "status_code", None)
    if isinstance(status_code, int) and 100 <= status_code <= 599:
        return f"http_{status_code}"
    return type(error).__name__


def _session_factory_for(database_url: str) -> Callable[[], AsyncSession]:
    """Build an isolated async session factory from one injected URL.

    Used only when a caller injects the selected profile's database URL, so
    the run never consults unrelated DATABASE_URL configuration.
    """
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    url = database_url
    if url.startswith("postgresql://") and not url.startswith("postgresql+asyncpg://"):
        url = url.replace("postgresql://", "postgresql+asyncpg://", 1)
    engine = create_async_engine(url, pool_pre_ping=True)
    return async_sessionmaker(engine, expire_on_commit=False)


def build_emitter(
    run_id: str,
    case_id: str,
    root: Path,
    external_sink: EventSink | None,
) -> EventEmitter:
    """Build the run emitter: persistent JSONL + display memory + external sink.

    The external display/renderer sink is wrapped in a :class:`NonFatalSink`:
    a renderer exception must never change business execution or persistence.
    The first renderer failure records one safe notice; persistence failures
    remain fatal and explicit.
    """
    persistent = JsonlPersistentSink(run_id, case_id, root=root)
    sinks: list[EventSink] = [persistent, DisplayMemory()]
    emitter = EventEmitter(run_id, case_id, sinks)
    if external_sink is not None:
        guard = NonFatalSink(external_sink)

        def notice(error: Exception) -> None:
            if guard.error_count == 1:
                emitter.emit(
                    EventKind.ERROR,
                    EventSource.ENGINE,
                    text=f"renderer notice: {type(error).__name__}",
                    error="renderer",
                )

        guard.set_on_error(notice)
        emitter.add(guard)
    return emitter


def project_tool(
    projector: ToolProjector | None,
    flow_id: str,
    tool: str,
    arguments: Mapping[str, object],
) -> str:
    """Return a display-safe label for one tool call.

    Unknown tools (or no projector) fall back to a details-hidden label so
    argument values never leak into the timeline.
    """
    if projector is not None:
        return projector.project(flow_id, tool, arguments).label
    return f"{tool} (details hidden)"


def project_result(
    projector: ToolProjector | None,
    flow_id: str,
    tool: str,
    result: str,
) -> str:
    """Return a display-safe summary for one tool result.

    Raw tool return text is model-internal context only; it never reaches a
    DisplayEvent or the persistent JSONL.  Unprojected results render as
    ``(result details hidden)``.
    """
    if projector is not None:
        return projector.project_result(flow_id, tool, result)
    return "(result details hidden)"


def _emit_final_done(
    emitter: EventEmitter,
    source: EventSource,
    report: SimulatorReport,
) -> None:
    """Emit the final DONE boundary with the run summary."""
    cost = f" cost=${report.cost_usd:.4f}" if report.cost_usd is not None else ""
    emitter.emit(
        EventKind.DONE,
        source,
        text=(
            f"{report.end_reason} verified={report.verified_goal} "
            f"turns={report.turns} tokens={report.total_tokens}{cost}"
        ),
        outcome=report.end_reason,
        verified=report.verified_goal,
        turns=report.turns,
        tokens=report.total_tokens,
        latency_ms=round(report.total_latency_ms, 2),
    )


@dataclass(frozen=True)
class ConversationResult:
    report: SimulatorReport
    transcript_path: Path
    report_path: Path


class PersonaConversation:
    """One shared two-sided conversation loop.

    ``agent_turn`` is the real product agent callback.  The persona model and
    product model are both hosted Luna calls; only termination and state checks
    are deterministic code.
    """

    def __init__(
        self,
        persona: PersonaDefinition,
        agent_turn: Callable[[str, bool], Awaitable[str]],
        *,
        run_id: str | None = None,
        max_turns: int = DEFAULT_MAX_TURNS,
        root: Path = Path("artifacts/user-simulator"),
        usage: _UsageTotals | None = None,
        events: EventEmitter | None = None,
        agent_source: EventSource = EventSource.SUPPORT,
    ) -> None:
        if max_turns < 1:
            raise ValueError("max_turns must be positive")
        self.persona, self.agent_turn, self.max_turns = persona, agent_turn, max_turns
        self.run_id = run_id or uuid4().hex
        self.root = root
        self.report_path = self.root / f"{self.run_id}.json"
        self.transcript_path = self.root / f"{self.run_id}.jsonl"
        self.usage = usage if usage is not None else _UsageTotals()
        self.agent_source = agent_source
        self.last_report: SimulatorReport | None = None
        self.events = (
            events
            if events is not None
            else build_emitter(self.run_id, persona.persona_id, root, None)
        )

    def _emit_start(self) -> None:
        """Announce run identity and artifact paths before any model call."""
        self.events.emit(
            EventKind.START,
            EventSource.ENGINE,
            text=f"starting {self.persona.kind} case {self.persona.persona_id}",
            detail=(
                f"run_id={self.run_id}",
                f"jsonl_path={self.transcript_path}",
                f"report_path={self.report_path}",
            ),
        )

    def _emit_model(
        self, source: EventSource, *, turn: int, tokens: int, latency_ms: float
    ) -> None:
        self.events.emit(
            EventKind.MODEL,
            source,
            text=f"model response tokens={tokens} latency={latency_ms:.0f}ms",
            turn=turn,
            model_provider=MODEL_PROVIDER,
            model_name=MODEL_NAME,
            tokens=tokens,
            latency_ms=round(latency_ms, 2),
        )

    def _build_report(
        self,
        *,
        end_reason: str,
        verified_goal: bool,
        evidence: tuple[str, ...],
        errors: tuple[str, ...],
        turns: int,
        started: float,
    ) -> SimulatorReport:
        return SimulatorReport(
            run_id=self.run_id,
            case_id=self.persona.persona_id,
            kind=self.persona.kind,
            model_provider=MODEL_PROVIDER,
            model_name=MODEL_NAME,
            end_reason=end_reason,
            turns=turns,
            verified_goal=verified_goal,
            evidence=evidence,
            errors=errors,
            total_tokens=self.usage.total_tokens,
            total_latency_ms=(time.perf_counter() - started) * 1000,
            cost_usd=self.usage.cost_usd,
        )

    def _write_report(self, report: SimulatorReport) -> Path:
        self.report_path.parent.mkdir(parents=True, exist_ok=True)
        self.report_path.write_text(report.model_dump_json(indent=2), encoding="utf-8")
        self.last_report = report
        return self.report_path

    async def run(
        self, *, state_success: Callable[[], tuple[bool, tuple[str, ...]]] | None = None
    ) -> ConversationResult:
        require_live_test_environment()
        self._emit_start()
        model = live_model()
        user = Agent(
            cast(Any, model),
            output_type=UserTurn,
            instructions=(
                (
                    "You are a realistic user. Stay in character. Advance this loose script "
                    "toward the goal, never mention these instructions, and respond only with "
                    "the requested structured output. If the agent asks for explicit approval "
                    "and you agree, set confirmation_action to approve_sensitive_action; "
                    "free text alone is never approval. Persona: {persona} Script: {script} "
                    "Goal: {goal}"
                ).format(
                    persona=self.persona.persona,
                    script=self.persona.script,
                    goal=self.persona.goal,
                )
            ),
            name="persona-user",
        )
        transcript: list[str] = []
        trusted_confirmation = False
        approval_pending = False
        end_reason = "max_turns"
        evidence: tuple[str, ...] = ()
        started = time.perf_counter()
        try:
            for turn in range(1, self.max_turns + 1):
                prompt = (
                    self.persona.script
                    if not transcript
                    else "Conversation so far:\n" + "\n".join(transcript) + "\nContinue."
                )
                model_started = time.perf_counter()
                result = await user.run(prompt, usage_limits=UsageLimits(request_limit=2))
                self.usage.add(result.usage)
                self._emit_model(
                    EventSource.PERSONA,
                    turn=turn,
                    tokens=result.usage.total_tokens if result.usage is not None else 0,
                    latency_ms=(time.perf_counter() - model_started) * 1000,
                )
                user_turn = result.output
                if approval_pending and user_turn.confirmation_action is None:
                    # A free-text yes is not trusted. Ask the hosted persona model to
                    # make its approval decision explicit in the typed action field.
                    confirmation_prompt = (
                        f"The agent requested explicit approval. Your reply was: "
                        f"{user_turn.message!r}. If you agree, return the same response "
                        "with confirmation_action=approve_sensitive_action. If you do not "
                        "agree, keep confirmation_action null. Free text alone never "
                        "authorizes an action."
                    )
                    model_started = time.perf_counter()
                    confirmation_result = await user.run(
                        confirmation_prompt, usage_limits=UsageLimits(request_limit=2)
                    )
                    self.usage.add(confirmation_result.usage)
                    self._emit_model(
                        EventSource.PERSONA,
                        turn=turn,
                        tokens=(
                            confirmation_result.usage.total_tokens
                            if confirmation_result.usage is not None
                            else 0
                        ),
                        latency_ms=(time.perf_counter() - model_started) * 1000,
                    )
                    user_turn = confirmation_result.output
                transcript.append("user: " + user_turn.message)
                self.events.emit(
                    EventKind.USER,
                    EventSource.PERSONA,
                    text=user_turn.message,
                    turn=turn,
                )
                trusted_confirmation = (
                    approval_pending and user_turn.confirmation_action is not None
                )
                answer = await self.agent_turn(user_turn.message, trusted_confirmation)
                answer_lower = answer.lower()
                approval_pending = (
                    "confirmation.required" in answer_lower
                    or "explicit confirmation" in answer_lower
                    or ("confirmation" in answer_lower and "required" in answer_lower)
                    or ("approval" in answer_lower and "required" in answer_lower)
                )
                transcript.append("agent: " + answer)
                self.events.emit(
                    EventKind.AGENT,
                    self.agent_source,
                    text=answer,
                    turn=turn,
                )
                if state_success is not None:
                    verified, evidence = state_success()
                    if verified:
                        end_reason = "state_verified_success"
                        break
                if user_turn.goal_reached:
                    # The model may request evaluation, but only observed state can pass.
                    end_reason = "user_goal_claim_unverified"
                if any(
                    x in answer.lower()
                    for x in ("conversation ended", "goodbye", "ticket created", "completed")
                ):
                    end_reason = "agent_end"
                    break
            if end_reason == "user_goal_claim_unverified":
                end_reason = "max_turns"
            turns = (len(transcript) + 1) // 2
            report = self._build_report(
                end_reason=end_reason,
                verified_goal=end_reason == "state_verified_success",
                evidence=evidence,
                errors=(),
                turns=turns,
                started=started,
            )
        except asyncio.CancelledError:
            # Partial result on cancellation: the caller (engine) still owns
            # cleanup, but the report and boundary events are written here so
            # a Ctrl-C after start never loses the run's partial outcome.
            turns = (len(transcript) + 1) // 2
            partial = self._build_report(
                end_reason="cancelled",
                verified_goal=False,
                evidence=evidence,
                errors=("interrupted by user (Ctrl-C)",),
                turns=turns,
                started=started,
            )
            self._write_report(partial)
            self.events.emit(
                EventKind.ERROR,
                EventSource.ENGINE,
                text="run interrupted before completion",
                reason="interrupted",
            )
            raise
        except Exception as error:
            turns = (len(transcript) + 1) // 2
            failure_reason = _safe_failure_reason(error)
            partial = self._build_report(
                end_reason="error",
                verified_goal=False,
                evidence=evidence,
                errors=(f"run failed: {failure_reason}",),
                turns=turns,
                started=started,
            )
            self._write_report(partial)
            self.events.emit(
                EventKind.ERROR,
                EventSource.ENGINE,
                text=f"run failed: {failure_reason}",
                error=failure_reason,
            )
            raise
        self._write_report(report)
        return ConversationResult(report, self.transcript_path, self.report_path)


async def run_support(
    persona: PersonaDefinition,
    *,
    max_turns: int = DEFAULT_MAX_TURNS,
    root: Path = Path("artifacts/user-simulator"),
    event_sink: EventSink | None = None,
    tool_projector: ToolProjector | None = None,
    database_url: str | None = None,
) -> ConversationResult:
    """Run a support persona in a seeded PostgreSQL transaction and always roll it back.

    ``database_url`` lets the caller inject the selected profile's disposable
    database; when set, it is used for both the sandbox session and the
    provisioner target, and unrelated configuration is never consulted.
    """
    if persona.kind != "support":
        raise ValueError("run_support requires a support persona")
    require_live_test_environment()
    run_id = uuid4().hex
    emitter = build_emitter(run_id, persona.persona_id, root, event_sink)
    flow_id = persona.scenario_or_workflow_id
    from app.adapters.pydantic_ai_agent import PydanticAISupportAgent
    from app.config import get_settings
    from app.db import get_session_factory
    from app.domain.agent.schemas import SupportRequest
    from app.domain.bundle.compiler import compile_bundle
    from app.domain.bundle.extract import synthetic_id
    from app.domain.simulation.adapters import SUPPORT_DATABASE_COVERAGE, StateMutation
    from app.domain.simulation.events import SimulationEventCollector
    from app.domain.simulation.provisioner import EnvironmentRequest, postgres_provisioner_factory
    from app.domain.simulation.runner import scenario_from_bundle
    from app.domain.simulation.scenarios import SCENARIO_BY_ID as SIMULATION_SCENARIOS
    from app.telemetry.recorder import TraceRecorder, TraceSpan

    source = SIMULATION_SCENARIOS[persona.scenario_or_workflow_id]

    # The bundle compiler replaces source identifiers with stable synthetic IDs.
    def portable_script(value: str) -> str:
        return _UUID_PATTERN.sub(lambda match: str(synthetic_id(UUID(match.group(0)))), value)

    simulation_persona = persona.model_copy(update={"script": portable_script(persona.script)})
    bundle = compile_bundle(
        scenario=source,
        approved_request_message=simulation_persona.script,
        reviewer="live-simulator",
        reviewed_at="2026-01-01T00:00:00Z",
        reason="approved fixed persona",
        review_status="approved",
        coverage_items=(SUPPORT_DATABASE_COVERAGE,),
    )
    scenario = scenario_from_bundle(bundle)
    settings = get_settings()
    target_url = database_url if database_url is not None else str(settings.database_url)
    session_factory = (
        _session_factory_for(target_url) if database_url is not None else get_session_factory()
    )
    factory = postgres_provisioner_factory(
        session_factory,
        database_url=target_url,
        environment=settings.environment,
        isolation_confirmed=True,
    )
    collector = SimulationEventCollector()
    provisioner = factory(EnvironmentRequest(scenario=scenario, fault_script=None, sink=collector))
    latest = None
    latest_state = None
    usage_totals = _UsageTotals()

    def support_span_listener(span: TraceSpan, ended: bool) -> None:
        usage_totals.add_model_span(span.name, span.attributes, ended=ended)
        if not ended:
            return
        if span.name == "support_agent.retry":
            attempts = span.attributes.get("support.retry.count")
            emitter.emit(
                EventKind.RETRY,
                EventSource.SUPPORT,
                text=f"retrying tool call attempt={attempts}",
                tool="",
                attempts=attempts if isinstance(attempts, int) else None,
            )
            return
        if span.name.startswith("support_agent.tool."):
            tool = str(span.attributes.get("tool.name") or span.name.rsplit(".", 1)[-1])
            label = project_tool(
                tool_projector,
                flow_id,
                tool,
                {"order_id": span.attributes.get("tool.order.id")},
            )
            emitter.emit(
                EventKind.TOOL_SELECTED,
                EventSource.SUPPORT,
                text=label,
                tool=tool,
                outcome="selected",
            )
            if span.error_code:
                emitter.emit(
                    EventKind.TOOL_RESULT,
                    EventSource.SUPPORT,
                    text=f"{label} error={span.error_code}",
                    tool=tool,
                    outcome="error",
                    error=span.error_code,
                )
            else:
                emitter.emit(
                    EventKind.TOOL_RESULT,
                    EventSource.SUPPORT,
                    text=f"{label} ok",
                    tool=tool,
                    outcome="ok",
                )
            return
        if span.name in {"support_agent.routing", "support_agent.answer"}:
            total = span.attributes.get("model.tokens.total")
            latency = span.attributes.get("model.latency.ms")
            emitter.emit(
                EventKind.MODEL,
                EventSource.SUPPORT,
                text=(
                    f"model {span.name} tokens={total} "
                    f"latency={latency if latency is not None else '?'}ms"
                ),
                model_provider=MODEL_PROVIDER,
                model_name=MODEL_NAME,
                tokens=total if isinstance(total, int) else None,
                latency_ms=round(float(latency), 2) if isinstance(latency, (int, float)) else None,
            )

    async def turn(message: str, confirmed: bool) -> str:
        nonlocal latest, latest_state
        request = SupportRequest(
            customer_id=scenario.request.customer_id,
            message=message,
            refund_confirmed=confirmed,
        )
        if product_agent is None:
            raise RuntimeError("support agent was not initialized")
        latest = await product_agent.handle(request)
        latest_state = await provisioner.final_state()
        return latest.message

    def transition_allowed(mutation: StateMutation) -> bool:
        resource = mutation.resource
        resource_id = mutation.resource_id
        field = mutation.field
        before = mutation.before
        after = mutation.after
        reason_code = mutation.reason_code
        for expected in scenario.expected_behavior.permitted_state_transitions:
            if expected.resource != resource or expected.reason_code != reason_code:
                continue
            if not expected.any_resource_id and expected.resource_id is not None:
                if str(expected.resource_id) != resource_id:
                    continue
            if expected.to_status == "created" and field == "created":
                return expected.from_status is None
            if field != "status":
                continue
            if expected.from_status != before or expected.to_status != after:
                continue
            return True
        return False

    def success() -> tuple[bool, tuple[str, ...]]:
        if latest is None or latest_state is None:
            return False, ()
        expected = scenario.expected_behavior
        mutations = provisioner.mutations()
        transitions = tuple(
            f"{m.resource}:created"
            if m.field == "created"
            else f"{m.resource}:{m.before}->{m.after}"
            for m in mutations
            if m.field in {"created", "status"}
        )
        emitter.emit(
            EventKind.STATE,
            EventSource.SUPPORT,
            text=f"state: {'; '.join(transitions) if transitions else 'none'}",
            transition="; ".join(transitions),
        )
        safe = all(transition_allowed(mutation) for mutation in mutations)
        required = expected.state_transitions
        observed_required = tuple(
            expected_transition.reason_code
            for expected_transition in required
            if any(
                mutation.reason_code == expected_transition.reason_code
                and (
                    expected_transition.any_resource_id
                    or str(expected_transition.resource_id) == mutation.resource_id
                )
                for mutation in mutations
            )
        )
        complete = len(observed_required) == len(required)
        return (
            latest.outcome is expected.outcome
            and latest.reason_code in expected.reason_codes
            and safe
            and complete
        ), observed_required

    product_agent: PydanticAISupportAgent | None = None
    conversation: PersonaConversation | None = None
    try:
        await provisioner.create()
        await provisioner.seed(scenario.initial_state)
        product_agent = PydanticAISupportAgent(
            model_config=ModelConfig(
                provider="openai",
                name=MODEL_NAME,
                base_url=settings.model_base_url,
                api_key=settings.model_api_key,
            ),
            recorder=TraceRecorder(None, span_listener=support_span_listener),
            repository=provisioner.connect(),
        )
        conversation = PersonaConversation(
            simulation_persona,
            turn,
            max_turns=max_turns,
            root=root,
            usage=usage_totals,
            events=emitter,
            agent_source=EventSource.SUPPORT,
            run_id=run_id,
        )
        return await conversation.run(state_success=success)
    finally:
        try:
            await provisioner.destroy()
        except Exception as error:  # noqa: BLE001 - reported as an explicit marker
            emitter.emit(
                EventKind.CLEANUP,
                EventSource.SUPPORT,
                text=f"cleanup failed: {type(error).__name__}",
                reason="cleanup_failed",
            )
            raise RuntimeError(f"cleanup failed: {type(error).__name__}") from error
        emitter.emit(
            EventKind.CLEANUP,
            EventSource.SUPPORT,
            text="support database rolled back",
            reason="rollback",
        )
        if conversation is not None and conversation.last_report is not None:
            _emit_final_done(emitter, EventSource.SUPPORT, conversation.last_report)


async def run_reference(
    persona: PersonaDefinition,
    *,
    max_turns: int = DEFAULT_MAX_TURNS,
    root: Path = Path("artifacts/user-simulator"),
    event_sink: EventSink | None = None,
    tool_projector: ToolProjector | None = None,
) -> ConversationResult:
    """Run a reference workflow using its real registered tools and repository."""
    require_live_test_environment()
    run_id = uuid4().hex
    emitter = build_emitter(run_id, persona.persona_id, root, event_sink)
    flow_id = persona.scenario_or_workflow_id
    from app.domain.reference.workflows.six_reference import ALL_WORKFLOWS

    workflow = next(
        (w for w in ALL_WORKFLOWS if w.workflow_id == persona.scenario_or_workflow_id), None
    )
    if workflow is None:
        raise ValueError(f"unknown reference workflow: {persona.scenario_or_workflow_id}")
    repository = workflow.repository
    repository.seed(workflow.seed_state)
    model = live_model()
    baseline_steps = ", ".join(
        f"{step.tool}({json.dumps(step.arguments, sort_keys=True)})"
        for step in workflow.baseline_plan.tool_calls
    )
    usage_totals = _UsageTotals()
    choices = Agent(
        cast(Any, model),
        output_type=BusinessChoice,
        instructions=(
            "You operate a business workflow. Choose only one listed tool per response, "
            "use observed state, and request approval before sensitive tools. Do not set "
            "end=true until every required transition is observed. Required transitions: "
            + ", ".join(workflow.expectation.required_transitions)
            + ". Reviewed baseline tool sequence (use its arguments when applicable): "
            + baseline_steps
            + ". Goal: "
            + persona.goal
            + " Tools: "
            + ", ".join(t.name for t in workflow.tools)
        ),
        name="reference-business-agent",
    )
    tool_map = {t.name: t for t in workflow.tools}

    confirmed = False
    approval_pending = False
    pending_action: tuple[str, dict[str, object]] | None = None
    executed_tools: list[str] = []

    def next_reviewed_step() -> str:
        for step in workflow.baseline_plan.tool_calls:
            if step.tool not in executed_tools:
                return f"{step.tool} with arguments {json.dumps(step.arguments, sort_keys=True)}"
        return "the next allowed tool based on observed state"

    def progress() -> str:
        transitions = tuple(
            f"{m.get('resource')}:created"
            if m.get("field") == "created"
            else f"{m.get('resource')}:{m.get('before', '?')}->{m.get('after', '?')}"
            for m in repository.mutations()
            if m.get("field") in {"created", "status"}
        )
        return "; ".join(transitions) if transitions else "none"

    def required_done() -> bool:
        observed = {
            (
                f"{m.get('resource')}:created"
                if m.get("field") == "created"
                else f"{m.get('resource')}:{m.get('before', '?')}->{m.get('after', '?')}"
            )
            for m in repository.mutations()
            if m.get("field") in {"created", "status"}
        }
        return set(workflow.expectation.required_transitions).issubset(observed)

    def _emit_state() -> None:
        transitions = tuple(
            f"{m.get('resource')}:created"
            if m.get("field") == "created"
            else f"{m.get('resource')}:{m.get('before', '?')}->{m.get('after', '?')}"
            for m in repository.mutations()
            if m.get("field") in {"created", "status"}
        )
        emitter.emit(
            EventKind.STATE,
            EventSource.REFERENCE,
            text=f"state: {'; '.join(transitions) if transitions else 'none'}",
            transition="; ".join(transitions),
        )

    async def run_tool(tool_name: str, arguments: dict[str, object]) -> str:
        tool = tool_map.get(tool_name)
        if tool is None:
            raise KeyError(tool_name)
        label = project_tool(tool_projector, flow_id, tool_name, arguments)
        emitter.emit(
            EventKind.TOOL_SELECTED,
            EventSource.REFERENCE,
            text=label,
            tool=tool_name,
            outcome="selected",
        )
        try:
            result = str(tool.run(repository, arguments))
        except (TimeoutError, ConnectionError) as exc:
            # Retry the exact same call, not a fresh model decision.
            emitter.emit(
                EventKind.RETRY,
                EventSource.REFERENCE,
                text=f"{label} {type(exc).__name__}; retrying the same call",
                tool=tool_name,
                attempts=2,
            )
            try:
                result = str(tool.run(repository, arguments))
            except (TimeoutError, ConnectionError):
                emitter.emit(
                    EventKind.TOOL_RESULT,
                    EventSource.REFERENCE,
                    text=f"{label} failed ({type(exc).__name__})",
                    tool=tool_name,
                    outcome="error",
                    error=type(exc).__name__,
                )
                return f"Temporary tool failure: {type(exc).__name__}"
        summary = project_result(tool_projector, flow_id, tool_name, result)
        emitter.emit(
            EventKind.TOOL_RESULT,
            EventSource.REFERENCE,
            text=f"{label} -> {summary}",
            tool=tool_name,
            outcome="ok",
        )
        return result

    async def agent_turn(message: str, trusted_confirmation: bool = False) -> str:
        nonlocal confirmed, approval_pending, pending_action
        context = (
            f"User message: {message}\n"
            f"Observed state transitions: {progress()}.\n"
            "Continue the workflow with an allowed tool; never infer a mutation "
            "from the user's free text."
        )
        if trusted_confirmation and approval_pending and pending_action is not None:
            confirmed = True
            approval_pending = False
            tool_name, arguments = pending_action
            pending_action = None
            result = await run_tool(tool_name, arguments)
            executed_tools.append(tool_name)
            context = (
                f"Trusted confirmation action executed {tool_name}: {result}. "
                f"Observed state transitions: {progress()}. Completed tools: "
                f"{tuple(executed_tools)}. Continue with {next_reviewed_step()}."
            )
        for _ in range(8):
            business_result = await choices.run(context, usage_limits=UsageLimits(request_limit=2))
            usage_totals.add(business_result.usage)
            choice = business_result.output
            if choice.end or choice.tool is None:
                if required_done() and (not workflow.expectation.gate_required or confirmed):
                    return choice.message
                context = (
                    "You tried to end before the workflow goal was verified. "
                    f"Required transitions are {workflow.expectation.required_transitions}; "
                    f"observed transitions are {progress()}. Choose the next allowed "
                    "tool now and do not end."
                )
                continue
            if choice.tool in executed_tools:
                emitter.emit(
                    EventKind.TOOL_RESULT,
                    EventSource.REFERENCE,
                    text=f"{choice.tool} already succeeded; skipping repeat",
                    tool=choice.tool,
                    outcome="skipped",
                )
                context = (
                    f"Do not repeat {choice.tool}; it already succeeded. Completed tools: "
                    f"{tuple(executed_tools)}. Choose {next_reviewed_step()} now."
                )
                continue
            tool = tool_map.get(choice.tool)
            if tool is None:
                emitter.emit(
                    EventKind.TOOL_RESULT,
                    EventSource.REFERENCE,
                    text=f"{choice.tool} is not an allowed tool",
                    tool=choice.tool,
                    outcome="rejected",
                )
                context = (
                    f"Tool {choice.tool!r} is not allowed. Choose exactly one of "
                    f"{tuple(tool_map)} and continue."
                )
                continue
            requires_confirmation = workflow.expectation.gate_required and (
                choice.tool in workflow.expectation.protected_tools or not tool.safe
            )
            if requires_confirmation and not confirmed:
                approval_pending = True
                pending_action = (choice.tool, dict(choice.arguments))
                emitter.emit(
                    EventKind.APPROVAL,
                    EventSource.REFERENCE,
                    text=f"approval required before {choice.tool}",
                    tool=choice.tool,
                    reason="protected tool",
                )
                return (
                    f"Approval is required before {choice.tool}. Please provide explicit "
                    "confirmation using the trusted confirmation action."
                )
            result = await run_tool(choice.tool, dict(choice.arguments))
            executed_tools.append(choice.tool)
            context = (
                f"Tool result for {choice.tool}: {result}. "
                f"Observed state transitions: {progress()}. Completed tools: "
                f"{tuple(executed_tools)}. Continue with {next_reviewed_step()}."
            )
        return "The workflow is still in progress; continue with the next user turn."

    def success() -> tuple[bool, tuple[str, ...]]:
        observation = workflow.observer(repository.snapshot(), repository.mutations())
        transitions = tuple(
            f"{m.get('resource')}:created"
            if m.get("field") == "created"
            else f"{m.get('resource')}:{m.get('before', '?')}->{m.get('after', '?')}"
            for m in repository.mutations()
            if m.get("field") in {"created", "status"}
        )
        required = set(workflow.expectation.required_transitions)
        permitted = set(workflow.expectation.permitted_transitions)
        safe = all(t in permitted for t in transitions)
        complete = required.issubset(set(transitions))
        gate = (not workflow.expectation.gate_required) or confirmed
        _emit_state()
        return (
            observation.outcome == workflow.expectation.outcome and complete and safe and gate,
            tuple(sorted(set(transitions) & required)),
        )

    conversation = PersonaConversation(
        persona,
        agent_turn,
        max_turns=max_turns,
        root=root,
        usage=usage_totals,
        events=emitter,
        agent_source=EventSource.REFERENCE,
        run_id=run_id,
    )
    try:
        return await conversation.run(state_success=success)
    finally:
        try:
            repository.destroy()
        except Exception as error:  # noqa: BLE001 - reported as an explicit marker
            emitter.emit(
                EventKind.CLEANUP,
                EventSource.REFERENCE,
                text=f"cleanup failed: {type(error).__name__}",
                reason="cleanup_failed",
            )
            raise RuntimeError(f"cleanup failed: {type(error).__name__}") from error
        emitter.emit(
            EventKind.CLEANUP,
            EventSource.REFERENCE,
            text="reference repository destroyed",
            reason="destroyed",
        )
        if conversation.last_report is not None:
            _emit_final_done(emitter, EventSource.REFERENCE, conversation.last_report)

