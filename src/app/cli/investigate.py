"""CLI handlers and parser for `lab investigate` commands.

Implements:
- `lab investigate start <trace-ref> [--follow]`
- `lab investigate attach <id>`
- `lab investigate send <id> "<message>" [--steer]`
- `lab investigate list`
"""

import argparse
import asyncio
import json
from uuid import UUID

from rich.console import Console
from rich.table import Table

from app.config import get_settings
from app.db import get_session_factory
from app.domain.investigation.brief import render_task_brief
from app.domain.investigation.repository import SqlAlchemyInvestigationRepository
from app.domain.investigation.schemas import (
    TERMINAL_STATUSES,
    InvestigationCreateRequest,
)
from app.domain.investigation.service import InvestigationService
from app.domain.runner.schemas import (
    AgentArtifactRef,
    EnvironmentSliceRef,
    ResourceLimits,
)

console = Console(stderr=True)


async def _run_start(args: argparse.Namespace) -> None:
    settings = get_settings()
    session_factory = get_session_factory()
    async with session_factory() as session:
        repo = SqlAlchemyInvestigationRepository(session)
        service = InvestigationService(
            repo,
            control_plane_url=settings.control_plane_public_url,
        )

        trace_ref = (
            json.loads(args.trace_ref)
            if args.trace_ref.startswith("{")
            else {"trace_id": args.trace_ref}
        )

        slice_ref = EnvironmentSliceRef(
            world_id=getattr(args, "world_id", "support_world_v1"),
            world_version="1.0.0",
            slice_name=getattr(args, "slice_name", "investigation_slice"),
            fixture_bundle_ref=getattr(args, "fixture_bundle", "bundles/support_bundle.json"),
            provenance="observed",
        )

        investigator_ref = AgentArtifactRef(
            kind="prime_profile",
            digest_or_profile="prime-investigator@0.2.0",
        )

        task_brief = render_task_brief(
            trace_id=trace_ref.get("trace_id", "trace"),
            slice_ref=slice_ref,
        )

        create_req = InvestigationCreateRequest(
            trace_ref=trace_ref,
            world_ref={"world_id": slice_ref.world_id},
            slice_ref=slice_ref,
            investigator_ref=investigator_ref,
            task_brief=task_brief,
            resource_limits=ResourceLimits(timeout_s=settings.modal_timeout_s),
        )

        resp, token = await service.start(create_req)
        await session.commit()

        if getattr(args, "json", False):
            print(resp.model_dump_json(indent=2))
        else:
            print(f"Investigation started: {resp.id}")
            print(f"Status: {resp.status.value}")
            print(f"Bridge Token: {token}")

        if getattr(args, "follow", False):
            await _stream_events(
                service,
                resp.id,
                after_seq=0,
                json_output=getattr(args, "json", False),
            )


async def _stream_events(
    service: InvestigationService,
    investigation_id: UUID,
    *,
    after_seq: int = 0,
    json_output: bool = False,
) -> None:
    current_seq = after_seq
    while True:
        events = await service.get_events_stream(
            investigation_id,
            after_seq=current_seq,
            limit=50,
        )
        for event in events:
            current_seq = event.seq
            event_type = event.type.value if hasattr(event.type, "value") else str(event.type)
            if json_output:
                print(
                    json.dumps(
                        {
                            "seq": event.seq,
                            "type": event_type,
                            "payload": event.payload,
                            "emitted_at": event.emitted_at.isoformat(),
                        }
                    )
                )
            else:
                print(f"[{event.seq:03d}] {event_type.upper():<20} {json.dumps(event.payload)}")

        detail = await service.get_by_id(investigation_id)
        if detail.status in TERMINAL_STATUSES:
            if not json_output:
                print(f"--- Investigation reached terminal status: {detail.status.value} ---")
                if detail.summary:
                    print(f"Findings: {detail.summary.findings}")
                    print(f"Next Step: {detail.summary.next_step}")
            break
        await asyncio.sleep(0.5)


async def _run_attach(args: argparse.Namespace) -> None:
    settings = get_settings()
    session_factory = get_session_factory()
    inv_id = UUID(args.investigation_id)
    async with session_factory() as session:
        repo = SqlAlchemyInvestigationRepository(session)
        service = InvestigationService(repo, control_plane_url=settings.control_plane_public_url)
        await _stream_events(
            service,
            inv_id,
            after_seq=getattr(args, "last_seq", 0),
            json_output=getattr(args, "json", False),
        )


async def _run_send(args: argparse.Namespace) -> None:
    settings = get_settings()
    session_factory = get_session_factory()
    inv_id = UUID(args.investigation_id)
    mode = "steer" if getattr(args, "steer", False) else "prompt"

    async with session_factory() as session:
        repo = SqlAlchemyInvestigationRepository(session)
        service = InvestigationService(repo, control_plane_url=settings.control_plane_public_url)
        msg = await service.record_user_message(inv_id, body=args.message, mode=mode)
        await session.commit()

        if getattr(args, "json", False):
            print(json.dumps({
                "id": msg.id,
                "investigation_id": str(msg.investigation_id),
                "sender": msg.sender,
                "body": msg.body,
                "mode": msg.mode,
                "created_at": msg.created_at.isoformat(),
            }, indent=2))
        else:
            print(f"Message sent ({mode}): {args.message}")


async def _run_list(args: argparse.Namespace) -> None:
    settings = get_settings()
    session_factory = get_session_factory()
    async with session_factory() as session:
        repo = SqlAlchemyInvestigationRepository(session)
        service = InvestigationService(repo, control_plane_url=settings.control_plane_public_url)
        items = await service.list_investigations(limit=getattr(args, "limit", 50))

        if getattr(args, "json", False):
            print(json.dumps([item.model_dump(mode="json") for item in items], indent=2))
        else:
            table = Table(title="Recent Investigations")
            table.add_column("ID", style="cyan")
            table.add_column("Status", style="magenta")
            table.add_column("Created At", style="green")
            table.add_column("Error", style="red")

            for item in items:
                table.add_row(
                    str(item.id),
                    item.status.value,
                    item.created_at.strftime("%Y-%m-%d %H:%M:%S"),
                    item.error or "-",
                )
            console.print(table)


def cmd_investigate_start(args: argparse.Namespace) -> None:
    """CLI entrypoint for `lab investigate start`."""
    asyncio.run(_run_start(args))


def cmd_investigate_attach(args: argparse.Namespace) -> None:
    """CLI entrypoint for `lab investigate attach`."""
    asyncio.run(_run_attach(args))


def cmd_investigate_send(args: argparse.Namespace) -> None:
    """CLI entrypoint for `lab investigate send`."""
    asyncio.run(_run_send(args))


def cmd_investigate_list(args: argparse.Namespace) -> None:
    """CLI entrypoint for `lab investigate list`."""
    asyncio.run(_run_list(args))


def build_investigate_parser(parser: argparse.ArgumentParser) -> None:
    """Attach subcommands for `lab investigate`."""
    sub = parser.add_subparsers(dest="investigate_command", required=True)

    # lab investigate start <trace-ref> [--follow]
    start_p = sub.add_parser("start", help="start a new investigation from a trace")
    start_p.add_argument("trace_ref", help="trace ID or JSON trace reference")
    start_p.add_argument(
        "--follow",
        action="store_true",
        help="stream events immediately after starting",
    )
    start_p.add_argument("--world-id", default="support_world_v1", help="world ID")
    start_p.add_argument("--slice-name", default="support_slice", help="slice name")
    start_p.set_defaults(func=cmd_investigate_start)

    # lab investigate attach <id>
    attach_p = sub.add_parser("attach", help="attach and stream live events from an investigation")
    attach_p.add_argument("investigation_id", help="UUID of the investigation")
    attach_p.add_argument(
        "--last-seq",
        type=int,
        default=0,
        help="resume stream after this sequence number",
    )
    attach_p.set_defaults(func=cmd_investigate_attach)

    # lab investigate send <id> "<message>" [--steer]
    send_p = sub.add_parser("send", help="send a steering or follow-up message to the agent")
    send_p.add_argument("investigation_id", help="UUID of the investigation")
    send_p.add_argument("message", help="message content to send")
    send_p.add_argument("--steer", action="store_true", help="send message in steer mode")
    send_p.set_defaults(func=cmd_investigate_send)

    # lab investigate list
    list_p = sub.add_parser("list", help="list recent investigations")
    list_p.add_argument("--limit", type=int, default=50, help="max items to list")
    list_p.set_defaults(func=cmd_investigate_list)
