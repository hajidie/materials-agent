from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, Header, Query, Request, status
from pydantic import BaseModel, ConfigDict

from materialsagent.api.dependencies import get_actor_context, get_conversation_service, get_conversation_cleanup_service
from materialsagent.application.context import ActorContext
from materialsagent.application.conversations import ConversationService
from materialsagent.application.conversation_cleanup import ConversationCleanupService
from materialsagent.application.errors import ApplicationValidationError
from materialsagent.application.idempotency import validate_idempotency_key


router = APIRouter(prefix="/api/v1/conversations", tags=["conversations"])


def _utc_text(value: datetime) -> str:
    if (
        value.tzinfo is None
        or value.utcoffset() is None
        or value.utcoffset() != timezone.utc.utcoffset(value)
    ):
        raise ValueError("Public timestamps must be UTC.")
    return value.isoformat().replace("+00:00", "Z")


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ConversationCreateRequest(StrictModel):
    title: str | None = None


class ConversationView(StrictModel):
    conversation_id: str
    title: str | None
    created_at: str
    updated_at: str


class ConversationCreateData(ConversationView):
    idempotency_replayed: bool = False


class ConversationCreateResponse(StrictModel):
    request_id: str
    data: ConversationCreateData


class ConversationListItemView(ConversationView):
    last_activity_preview: str | None


class ConversationListData(StrictModel):
    items: list[ConversationListItemView]
    next_cursor: str | None


class ConversationListResponse(StrictModel):
    request_id: str
    data: ConversationListData


class ConversationDeleteData(StrictModel):
    conversation_id: str


class ConversationDeleteResponse(StrictModel):
    request_id: str
    data: ConversationDeleteData


@router.post(
    "",
    response_model=ConversationCreateResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_conversation(
    body: ConversationCreateRequest,
    request: Request,
    actor_context: Annotated[ActorContext, Depends(get_actor_context)],
    service: Annotated[
        ConversationService,
        Depends(get_conversation_service),
    ],
    idempotency_key: Annotated[
        str | None,
        Header(alias="Idempotency-Key"),
    ] = None,
) -> ConversationCreateResponse:
    try:
        validated_idempotency_key = validate_idempotency_key(idempotency_key)
    except (TypeError, ValueError):
        raise ApplicationValidationError() from None
    creation = service.create_idempotent(
        actor_context,
        body.title,
        request_id=request.state.request_id,
        idempotency_key=validated_idempotency_key,
    )
    conversation = creation.conversation
    return ConversationCreateResponse(
        request_id=request.state.request_id,
        data=ConversationCreateData(
            conversation_id=conversation.conversation_id,
            title=conversation.title,
            created_at=_utc_text(conversation.created_at),
            updated_at=_utc_text(conversation.updated_at),
            idempotency_replayed=creation.idempotency_replayed,
        ),
    )


@router.get("", response_model=ConversationListResponse)
def list_conversations(
    request: Request,
    actor_context: Annotated[ActorContext, Depends(get_actor_context)],
    service: Annotated[
        ConversationService,
        Depends(get_conversation_service),
    ],
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    cursor: str | None = None,
) -> ConversationListResponse:
    page = service.list(
        actor_context,
        limit=limit,
        cursor=cursor,
    )
    return ConversationListResponse(
        request_id=request.state.request_id,
        data=ConversationListData(
            items=[
                ConversationListItemView(
                    conversation_id=item.conversation_id,
                    title=item.title,
                    created_at=_utc_text(item.created_at),
                    updated_at=_utc_text(item.updated_at),
                    last_activity_preview=item.last_activity_preview,
                )
                for item in page.items
            ],
            next_cursor=page.next_cursor,
        ),
    )


@router.delete(
    "/{conversation_id}",
    response_model=ConversationDeleteResponse,
)
def delete_conversation(
    conversation_id: str,
    request: Request,
    background_tasks: BackgroundTasks,
    actor_context: Annotated[ActorContext, Depends(get_actor_context)],
    service: Annotated[
        ConversationCleanupService,
        Depends(get_conversation_cleanup_service),
    ],
) -> ConversationDeleteResponse:
    deletion = service.delete(actor_context, conversation_id)
    background_tasks.add_task(
        service.drain,
        limit=100,
        preferred_ids=deletion.cleanup_ids,
    )
    return ConversationDeleteResponse(
        request_id=request.state.request_id,
        data=ConversationDeleteData(conversation_id=conversation_id),
    )
