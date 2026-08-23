"""FastAPI route handlers for investigation management, chat, and event streaming."""

import asyncio
import json
from collections.abc import AsyncGenerator
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Query, Request, status
from fastapi.responses import StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.api.dependencies import get_investigation_service
from app.domain.investigation.schemas import (
    ChatMessageRequest,
    ChatMessageResponse,
    InboxMessagesResponse,
    InvestigationCreateRequest,
    InvestigationDetailResponse,
    InvestigationResponse,
    InvestigationStatus,
)
from app.domain.investigation.service import InvestigationService
from app.domain.runner.schemas import RunnerEvent

router = APIRouter(prefix="/api/v1/investigations", tags=["investigations"])
internal_router = APIRouter(prefix="/internal", tags=["internal"])
http_bearer = HTTPBearer(auto_error=True)


async def get_bridge_bearer_token(
    credentials: Annotated[HTTPAuthorizationCredentials, Depends(http_bearer)],
) -> str:
    """Extract bearer authentication token from request header."""
    return credentials.credentials


# ============================================================================
# Public API Routes (/api/v1/investigations)
# ============================================================================


@router.post(
    "",
    response_model=InvestigationResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def create_investigation(
    request: InvestigationCreateRequest,
    service: Annotated[InvestigationService, Depends(get_investigation_service)],
) -> InvestigationResponse:
    """Create a new investigation and provision resources."""
    resp, _ = await service.start(request)
    return resp


@router.get("", response_model=list[InvestigationResponse])
async def list_investigations(
    service: Annotated[InvestigationService, Depends(get_investigation_service)],
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[InvestigationResponse]:
    """List investigation summaries."""
    return await service.list_investigations(limit=limit, offset=offset)


@router.get("/{investigation_id}", response_model=InvestigationDetailResponse)
async def get_investigation_detail(
    investigation_id: UUID,
    service: Annotated[InvestigationService, Depends(get_investigation_service)],
) -> InvestigationDetailResponse:
    """Get detailed investigation state, including final summary if complete."""
    return await service.get_by_id(investigation_id)


@router.post(
    "/{investigation_id}/messages",
    response_model=ChatMessageResponse,
    status_code=status.HTTP_201_CREATED,
)
async def send_chat_message(
    investigation_id: UUID,
    request: ChatMessageRequest,
    service: Annotated[InvestigationService, Depends(get_investigation_service)],
) -> ChatMessageResponse:
    """Send a steering or prompt message to the running investigation agent."""
    msg = await service.record_user_message(
        investigation_id,
        body=request.body,
        mode=request.mode,
    )
    return ChatMessageResponse(
        id=msg.id,
        investigation_id=msg.investigation_id,
        sender=msg.sender,
        body=msg.body,
        mode=msg.mode,
        created_at=msg.created_at,
    )


@router.get("/{investigation_id}/events")
async def stream_investigation_events(
    investigation_id: UUID,
    request: Request,
    service: Annotated[InvestigationService, Depends(get_investigation_service)],
    last_event_id: Annotated[str | None, Header(alias="Last-Event-ID")] = None,
) -> StreamingResponse:
    """Stream investigation events using Server-Sent Events (SSE) with reconnect support.

    Replays any events newer than Last-Event-ID from PostgreSQL, then keeps the
    stream open and yields new arrivals.
    """
    # Verify investigation exists
    await service.get_by_id(investigation_id)

    last_seq = 0
    if last_event_id:
        try:
            last_seq = int(last_event_id)
        except ValueError:
            last_seq = 0

    async def event_generator() -> AsyncGenerator[str, None]:
        current_seq = last_seq

        while True:
            if await request.is_disconnected():
                break

            events = await service.get_events_stream(
                investigation_id,
                after_seq=current_seq,
                limit=50,
            )

            if events:
                for event in events:
                    current_seq = event.seq
                    data_json = json.dumps(event.payload)
                    yield f"id: {event.seq}\nevent: {event.type}\ndata: {data_json}\n\n"

            # Check if investigation has completed or failed
            detail = await service.get_by_id(investigation_id)
            if detail.status in {
                InvestigationStatus.COMPLETED,
                InvestigationStatus.FAILED,
                InvestigationStatus.CANCELLED,
            }:
                # Flush any remaining events
                trailing = await service.get_events_stream(
                    investigation_id,
                    after_seq=current_seq,
                    limit=50,
                )
                for event in trailing:
                    current_seq = event.seq
                    data_json = json.dumps(event.payload)
                    yield f"id: {event.seq}\nevent: {event.type}\ndata: {data_json}\n\n"
                break

            # Send heartbeat keepalive comment and poll delay
            yield ": keepalive\n\n"
            await asyncio.sleep(0.5)

    headers = {
        "Content-Type": "text/event-stream",
        "Cache-Control": "no-cache",
        "Connection": "keep-alive",
        "X-Accel-Buffering": "no",
    }
    return StreamingResponse(event_generator(), headers=headers)


# ============================================================================
# Internal In-Sandbox Bridge Routes (/internal)
# ============================================================================


@internal_router.get("/inbox", response_model=InboxMessagesResponse)
async def bridge_poll_inbox(
    token: Annotated[str, Depends(get_bridge_bearer_token)],
    service: Annotated[InvestigationService, Depends(get_investigation_service)],
    cursor: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> InboxMessagesResponse:
    """Long-poll inbox check for the in-sandbox bridge."""
    messages = await service.poll_inbox(token, cursor=cursor, limit=limit)
    next_cursor = cursor
    resp_messages: list[ChatMessageResponse] = []
    for msg in messages:
        resp_messages.append(
            ChatMessageResponse(
                id=msg.id,
                investigation_id=msg.investigation_id,
                sender=msg.sender,
                body=msg.body,
                mode=msg.mode,
                created_at=msg.created_at,
            )
        )
        if msg.id > next_cursor:
            next_cursor = msg.id

    return InboxMessagesResponse(messages=resp_messages, next_cursor=next_cursor)


@internal_router.post("/events", status_code=status.HTTP_200_OK)
async def bridge_record_events(
    events: list[RunnerEvent],
    token: Annotated[str, Depends(get_bridge_bearer_token)],
    service: Annotated[InvestigationService, Depends(get_investigation_service)],
) -> dict[str, Any]:
    """Ingest a batch of runner events from the in-sandbox bridge."""
    inserted = await service.record_events(token, events)
    return {"ok": True, "recorded": inserted}
