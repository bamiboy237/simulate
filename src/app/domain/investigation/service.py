"""Domain service coordinating investigation lifecycle, persistence, and state transitions."""

import asyncio
import hashlib
import re
import secrets
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit
from uuid import UUID, uuid4

from pydantic import SecretStr

from app.config import Settings, get_settings
from app.domain.investigation.brief import render_task_brief
from app.domain.investigation.errors import (
    InvalidBridgeTokenError,
    InvalidStateTransitionError,
    InvestigationNotFoundError,
    TerminalStateImmutableError,
)
from app.domain.investigation.models import (
    InvestigationEventRecord,
    InvestigationMessageRecord,
    InvestigationRecord,
)
from app.domain.investigation.repository import InvestigationRepository
from app.domain.investigation.schemas import (
    TERMINAL_STATUSES,
    InvestigationCreateRequest,
    InvestigationDetailResponse,
    InvestigationResponse,
    InvestigationStatus,
    InvestigationSummaryResponse,
)
from app.domain.runner.base import CloudRunner
from app.domain.runner.fake import FakeRunner
from app.domain.runner.modal_runner import ModalRunner
from app.domain.runner.schemas import (
    RESERVED_ENV_KEYS,
    RESERVED_SECRET_KEYS,
    EventType,
    ResourceLimits,
    RunnerEvent,
    SandboxHandle,
    SandboxSpec,
)

# Valid state machine transitions
ALLOWED_TRANSITIONS: dict[InvestigationStatus, set[InvestigationStatus]] = {
    InvestigationStatus.PENDING: {
        InvestigationStatus.PROVISIONING,
        InvestigationStatus.CANCELLED,
        InvestigationStatus.FAILED,
    },
    InvestigationStatus.PROVISIONING: {
        InvestigationStatus.RUNNING,
        InvestigationStatus.FAILED,
        InvestigationStatus.CANCELLED,
    },
    InvestigationStatus.RUNNING: {
        InvestigationStatus.COMPLETED,
        InvestigationStatus.FAILED,
        InvestigationStatus.CANCELLED,
    },
    InvestigationStatus.COMPLETED: set(),
    InvestigationStatus.FAILED: set(),
    InvestigationStatus.CANCELLED: set(),
}

# Hosts a cloud sandbox can never use as its control plane: loopback is local
# to the sandbox container, so a loopback callback URL silently breaks every
# cloud provisioning run.
MODAL_LOOPBACK_HOSTS: frozenset[str] = frozenset({"127.0.0.1", "localhost", "::1"})

# One DNS hostname label: letters, digits, and hyphens, never leading or
# trailing a hyphen. Bare domains only — no scheme, path, port, or wildcard.
DOMAIN_LABEL_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")


def hash_token(token: str) -> str:
    """Compute SHA-256 hash of a bearer token."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class InvestigationService:
    """Owns investigation state machine, event intake, chat, and stale sweep."""

    def __init__(
        self,
        repository: InvestigationRepository,
        *,
        runner: CloudRunner | None = None,
        control_plane_url: str | None = None,
    ) -> None:
        self._repository = repository
        self._runner = runner or self._resolve_runner()
        self._control_plane_url = control_plane_url
        self._handles: dict[UUID, SandboxHandle] = {}

    def _resolve_runner(self) -> CloudRunner:
        settings = get_settings()
        if settings.modal_enabled:
            return ModalRunner(
                app_name=settings.modal_app_name,
                default_timeout_s=settings.modal_timeout_s,
                default_outbound_domain_allowlist=list(settings.modal_outbound_domain_allowlist),
            )
        return FakeRunner()

    def _uses_modal(self) -> bool:
        """True when the resolved runner provisions Modal cloud sandboxes."""
        return isinstance(self._runner, ModalRunner)

    def _require_modal_control_plane_url(self) -> str:
        """Return a valid non-loopback HTTP(S) control-plane URL or raise."""
        raw = self._control_plane_url
        if not raw:
            raise ValueError(
                "CONTROL_PLANE_PUBLIC_URL is required when Modal is enabled; "
                "cloud sandboxes cannot reach a loopback control plane"
            )
        parsed = urlsplit(raw)
        if parsed.scheme not in ("http", "https"):
            raise ValueError("CONTROL_PLANE_PUBLIC_URL must use http or https")
        host = parsed.hostname
        if not host:
            raise ValueError("CONTROL_PLANE_PUBLIC_URL must include a hostname")
        if host.lower() in MODAL_LOOPBACK_HOSTS:
            raise ValueError(
                "CONTROL_PLANE_PUBLIC_URL must be a public URL when Modal is enabled; "
                "loopback hosts are not reachable from a cloud sandbox"
            )
        return raw

    @staticmethod
    def _require_bare_domain(entry: str) -> str:
        """Validate and normalize one outbound allowlist entry to a bare domain."""
        domain = entry.strip().lower()
        if not domain:
            raise ValueError("modal outbound domain allowlist entries cannot be empty")
        if len(domain) > 253:
            raise ValueError(f"invalid modal outbound domain entry {entry!r}: domain is too long")
        if "://" in domain or "/" in domain or ":" in domain:
            raise ValueError(
                f"invalid modal outbound domain entry {entry!r}: "
                "must be a bare domain name with no scheme, path, or port"
            )
        if not all(DOMAIN_LABEL_RE.fullmatch(label) for label in domain.split(".")):
            raise ValueError(
                f"invalid modal outbound domain entry {entry!r}: not a valid bare domain name"
            )
        return domain

    def _require_model_host(self, settings: Settings) -> str:
        """Derive the model-provider hostname the sandbox must reach."""
        base_url = settings.model_base_url
        if base_url:
            host = urlsplit(base_url).hostname
            if not host:
                raise ValueError(
                    "MODEL_BASE_URL must include a hostname to derive the "
                    "model-provider outbound domain"
                )
            return host.lower()
        if settings.model_provider == "openai":
            return "api.openai.com"
        raise ValueError(
            "Cannot derive the model-provider hostname for the outbound allowlist; "
            "set MODEL_BASE_URL or configure MODEL_PROVIDER=openai"
        )

    def _validate_modal_outbound_allowlist(
        self,
        settings: Settings,
        control_plane_host: str,
    ) -> list[str]:
        """Validate the configured allowlist and require the two mandatory hosts."""
        validated = [
            self._require_bare_domain(entry) for entry in settings.modal_outbound_domain_allowlist
        ]
        if not validated:
            raise ValueError(
                "MODAL_OUTBOUND_DOMAIN_ALLOWLIST must be configured when Modal is enabled"
            )
        required = {control_plane_host.lower(), self._require_model_host(settings)}
        missing = sorted(required - set(validated))
        if missing:
            raise ValueError(
                "MODAL_OUTBOUND_DOMAIN_ALLOWLIST must include the control-plane and "
                f"model-provider hostnames; missing: {missing}"
            )
        return validated

    async def start(
        self,
        request: InvestigationCreateRequest,
        *,
        investigation_id: UUID | None = None,
    ) -> tuple[InvestigationResponse, str]:
        """Create a new investigation in pending status and generate a bridge token."""
        # Validate caller-supplied environment and secret keys BEFORE persisting or provisioning
        env_conflicts = RESERVED_ENV_KEYS.intersection(request.env.keys())
        if env_conflicts:
            raise ValueError(
                f"Caller cannot override reserved environment variables: {sorted(env_conflicts)}"
            )

        secret_conflicts = RESERVED_SECRET_KEYS.intersection(request.secrets.keys())
        if secret_conflicts:
            raise ValueError(
                f"Caller cannot override reserved secret keys: {sorted(secret_conflicts)}"
            )

        inv_id = investigation_id or uuid4()
        token = secrets.token_urlsafe(32)
        token_hash = hash_token(token)

        # Resolve the callback URL, outbound policy, and watchdog budget BEFORE
        # persisting or provisioning. Modal sandboxes require a public control
        # plane and an explicit validated domain allowlist; FakeRunner keeps the
        # offline loopback default and an empty allowlist.
        runtime_settings = get_settings()
        if self._uses_modal():
            callback_url = self._require_modal_control_plane_url()
            control_plane_host = urlsplit(callback_url).hostname or ""
            outbound_domain_allowlist: list[str] = self._validate_modal_outbound_allowlist(
                runtime_settings,
                control_plane_host,
            )
            sandbox_secrets = dict(request.secrets)
            provider_secret_name = {
                "openai": "OPENAI_API_KEY",
                "anthropic": "ANTHROPIC_API_KEY",
            }.get(runtime_settings.model_provider or "")
            if provider_secret_name and runtime_settings.model_api_key:
                sandbox_secrets.setdefault(provider_secret_name, runtime_settings.model_api_key)
        else:
            callback_url = self._control_plane_url or "http://127.0.0.1:8000"
            outbound_domain_allowlist = []
            sandbox_secrets = dict(request.secrets)

        # Resolve a positive outer timeout so the watchdog budget can be
        # validated against the real Modal lifetime (a non-positive
        # resource_limits.timeout_s means "use the runner default").
        resolved_timeout_s = request.resource_limits.timeout_s
        if resolved_timeout_s <= 0:
            resolved_timeout_s = runtime_settings.modal_timeout_s
        resource_limits = ResourceLimits(
            timeout_s=resolved_timeout_s,
            cpus=request.resource_limits.cpus,
            memory_mib=request.resource_limits.memory_mib,
        )

        task_brief = request.task_brief or render_task_brief(
            trace_id=str(request.trace_ref.get("trace_id", "trace")),
            slice_ref=request.slice_ref,
        )

        tested_ref_dump = (
            request.tested_agent_ref.model_dump() if request.tested_agent_ref else None
        )

        # Build and validate the SandboxSpec BEFORE persisting any record. The
        # spec validates the resolved watchdog budget against the outer timeout,
        # so a violating combination (for example the 300s smoke sandbox with
        # default 600/300 watchdog timeouts) fails without creating a record.
        spec = SandboxSpec(
            spec_version="1.0.0",
            investigation_id=inv_id,
            task_brief=task_brief,
            environment_slice=request.slice_ref,
            investigator=request.investigator_ref,
            tested_agent=request.tested_agent_ref,
            trace_ref=dict(request.trace_ref),
            env=dict(request.env),
            secrets={"BRIDGE_TOKEN": SecretStr(token), **sandbox_secrets},
            callback_base_url=callback_url,
            resource_limits=resource_limits,
            tool_timeout_s=runtime_settings.tool_timeout_s,
            tool_recovery_timeout_s=runtime_settings.tool_recovery_timeout_s,
            outbound_domain_allowlist=outbound_domain_allowlist,
        )

        record = await self._repository.create(
            investigation_id=inv_id,
            status=InvestigationStatus.PENDING.value,
            trace_ref=request.trace_ref,
            world_ref=request.world_ref,
            slice_ref=request.slice_ref.model_dump(),
            investigator_ref=request.investigator_ref.model_dump(),
            tested_agent_ref=tested_ref_dump,
            task_brief=task_brief,
            bridge_token_hash=token_hash,
        )

        # Launch sandbox through the resolved runner
        try:
            handle = await asyncio.to_thread(self._runner.create_sandbox, spec)
            self._handles[inv_id] = handle
        except Exception as exc:
            await self._repository.update_status(
                inv_id,
                InvestigationStatus.FAILED.value,
                error=f"Sandbox creation failed: {exc}",
            )
            record.status = InvestigationStatus.FAILED.value
            record.error = f"Sandbox creation failed: {exc}"

        resp = InvestigationResponse(
            id=record.id,
            status=InvestigationStatus(record.status),
            task_brief=record.task_brief,
            bridge_token=token,
            created_at=record.created_at,
            started_at=record.started_at,
            finished_at=record.finished_at,
            error=record.error,
        )
        return resp, token

    async def transition_status(
        self,
        investigation_id: UUID,
        target_status: InvestigationStatus,
        *,
        error: str | None = None,
    ) -> InvestigationRecord:
        """Execute a state machine transition, rejecting invalid or terminal modifications."""
        record = await self._repository.get_by_id(investigation_id)
        if record is None:
            raise InvestigationNotFoundError(investigation_id)

        current_status = InvestigationStatus(record.status)
        if current_status in TERMINAL_STATUSES:
            raise TerminalStateImmutableError(current_status.value)

        if target_status not in ALLOWED_TRANSITIONS.get(current_status, set()):
            raise InvalidStateTransitionError(current_status.value, target_status.value)

        now = datetime.now(timezone.utc)
        started_at = now if target_status == InvestigationStatus.RUNNING else None
        finished_at = now if target_status in TERMINAL_STATUSES else None

        updated = await self._repository.update_status(
            investigation_id,
            target_status.value,
            error=error,
            started_at=started_at,
            finished_at=finished_at,
        )

        # Terminate sandbox on terminal state transitions
        if target_status in TERMINAL_STATUSES:
            handle = self._handles.get(investigation_id)
            if handle is not None:
                await asyncio.to_thread(self._runner.terminate, handle)

        if updated is None:
            raise InvestigationNotFoundError(investigation_id)
        return updated

    async def get_by_id(self, investigation_id: UUID) -> InvestigationDetailResponse:
        """Retrieve full investigation details."""
        record = await self._repository.get_by_id(investigation_id)
        if record is None:
            raise InvestigationNotFoundError(investigation_id)

        summary_resp: InvestigationSummaryResponse | None = None
        summary_record = await self._repository.get_summary(investigation_id)
        if summary_record is not None:
            summary_resp = InvestigationSummaryResponse(
                id=summary_record.id,
                investigation_id=summary_record.investigation_id,
                findings=summary_record.findings,
                next_step=summary_record.next_step,
                evidence_refs=summary_record.evidence_refs,
                created_at=summary_record.created_at,
            )

        return InvestigationDetailResponse(
            id=record.id,
            status=InvestigationStatus(record.status),
            task_brief=record.task_brief,
            trace_ref=record.trace_ref,
            world_ref=record.world_ref,
            slice_ref=record.slice_ref,
            investigator_ref=record.investigator_ref,
            tested_agent_ref=record.tested_agent_ref,
            error=record.error,
            last_heartbeat_at=record.last_heartbeat_at,
            created_at=record.created_at,
            started_at=record.started_at,
            finished_at=record.finished_at,
            summary=summary_resp,
        )

    async def list_investigations(
        self,
        *,
        limit: int = 50,
        offset: int = 0,
    ) -> list[InvestigationResponse]:
        """List investigation summaries."""
        records = await self._repository.list_investigations(limit=limit, offset=offset)
        return [
            InvestigationResponse(
                id=rec.id,
                status=InvestigationStatus(rec.status),
                task_brief=rec.task_brief,
                created_at=rec.created_at,
                started_at=rec.started_at,
                finished_at=rec.finished_at,
                error=rec.error,
            )
            for rec in records
        ]

    async def verify_bridge_token(self, token: str) -> InvestigationRecord:
        """Verify bearer token from bridge and return associated investigation."""
        token_hash = hash_token(token)
        record = await self._repository.get_by_bridge_token_hash(token_hash)
        if record is None:
            raise InvalidBridgeTokenError()
        return record

    async def record_events(
        self,
        token: str,
        events: list[RunnerEvent],
    ) -> int:
        """Persist a batch of events idempotently and update heartbeat/summary."""
        record = await self.verify_bridge_token(token)
        current_status = InvestigationStatus(record.status)
        if current_status in TERMINAL_STATUSES:
            # The active bridge may deliver its closing events (agent_end mapped
            # to investigation_finished, or a trailing error) in the batch
            # immediately after the summary batch that transitioned this
            # investigation to COMPLETED. Persist only those final events
            # idempotently; general writes to terminal investigations stay closed.
            if current_status == InvestigationStatus.COMPLETED and all(
                event.type in (EventType.investigation_finished, EventType.error)
                for event in events
            ):
                return await self._repository.record_events(record.id, events)
            return 0

        if current_status == InvestigationStatus.PENDING:
            await self.transition_status(record.id, InvestigationStatus.PROVISIONING)
            await self.transition_status(record.id, InvestigationStatus.RUNNING)
            record.status = InvestigationStatus.RUNNING.value
            current_status = InvestigationStatus.RUNNING
        elif current_status == InvestigationStatus.PROVISIONING:
            await self.transition_status(record.id, InvestigationStatus.RUNNING)
            record.status = InvestigationStatus.RUNNING.value
            current_status = InvestigationStatus.RUNNING

        now = datetime.now(timezone.utc)
        inserted = await self._repository.record_events(record.id, events)

        # Persist summary and heartbeat data first. A summary alone is not
        # terminal: the bridge contract requires the matching agent_end event
        # before the investigation can complete.
        summary_available = await self._repository.get_summary(record.id) is not None
        finished = False
        fatal_error: str | None = None
        for event in events:
            if event.type == EventType.heartbeat:
                await self._repository.update_heartbeat(record.id, now)
            elif event.type == EventType.summary_submitted:
                findings = str(event.payload.get("findings", "Investigation completed."))
                next_step = str(event.payload.get("next_step", "Apply suggested fixes."))
                evidence_refs = list(event.payload.get("evidence_refs", []))
                await self._repository.save_summary(
                    record.id,
                    findings=findings,
                    next_step=next_step,
                    evidence_refs=evidence_refs,
                )
                summary_available = True
            elif event.type == EventType.investigation_finished:
                finished = True
            elif event.type == EventType.error:
                fatal_error = str(
                    event.payload.get("error", "Agent encountered unrecoverable error.")
                )

        if fatal_error is not None:
            await self.transition_status(
                record.id,
                InvestigationStatus.FAILED,
                error=fatal_error,
            )
        elif finished:
            if summary_available:
                await self.transition_status(record.id, InvestigationStatus.COMPLETED)
            else:
                await self.transition_status(
                    record.id,
                    InvestigationStatus.FAILED,
                    error="Prime Agent ended without submitting a valid summary",
                )

        return inserted

    async def get_events_stream(
        self,
        investigation_id: UUID,
        *,
        after_seq: int = 0,
        limit: int = 100,
    ) -> list[InvestigationEventRecord]:
        """Fetch persisted events newer than sequence cursor for SSE replay and tailing."""
        return await self._repository.get_events(
            investigation_id,
            after_seq=after_seq,
            limit=limit,
        )

    async def record_user_message(
        self,
        investigation_id: UUID,
        body: str,
        mode: str = "prompt",
    ) -> InvestigationMessageRecord:
        """Store a chat message to be picked up by the bridge long-poll."""
        record = await self._repository.get_by_id(investigation_id)
        if record is None:
            raise InvestigationNotFoundError(investigation_id)
        if InvestigationStatus(record.status) in TERMINAL_STATUSES:
            raise TerminalStateImmutableError(record.status)

        return await self._repository.record_message(
            investigation_id,
            sender="user",
            body=body,
            mode=mode,
        )

    async def poll_inbox(
        self,
        token: str,
        *,
        cursor: int = 0,
        limit: int = 50,
    ) -> list[InvestigationMessageRecord]:
        """Return new chat messages for the bridge."""
        record = await self.verify_bridge_token(token)
        return await self._repository.get_messages(
            record.id,
            after_id=cursor,
            limit=limit,
        )

    async def sweep_stale(self, silence_seconds: int = 90) -> list[UUID]:
        """Mark running investigations silent for >silence_seconds as failed (operational)."""
        now = datetime.now(timezone.utc)
        cutoff = now - timedelta(seconds=silence_seconds)
        stale_records = await self._repository.find_stale_running(cutoff)
        reconciled_ids: list[UUID] = []

        for record in stale_records:
            try:
                await self.transition_status(
                    record.id,
                    InvestigationStatus.FAILED,
                    error="operational: heartbeat silence timeout",
                )
                reconciled_ids.append(record.id)
            except Exception:
                continue

        return reconciled_ids
