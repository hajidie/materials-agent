from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query, Request, status
from pydantic import BaseModel, ConfigDict

from materialsagent.api.dependencies import (
    get_actor_context,
    get_conversation_service,
    get_message_submission_service,
)
from materialsagent.application.context import ActorContext
from materialsagent.application.conversations import ConversationService
from materialsagent.application.messages import MessageSubmissionService


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


class ConversationCreateResponse(StrictModel):
    request_id: str
    data: ConversationView


class ConversationListItemView(ConversationView):
    last_activity_preview: str | None


class ConversationListData(StrictModel):
    items: list[ConversationListItemView]
    next_cursor: str | None


class ConversationListResponse(StrictModel):
    request_id: str
    data: ConversationListData


class MessageSubmissionRequest(StrictModel):
    submission_mode: Literal["NEW_TASK", "SUPPLEMENT_TASK"]
    content_text: str
    target_task_id: str | None = None


class UserMessageView(StrictModel):
    message_id: str
    role: Literal["USER"]
    content_text: str
    created_at: str


class PendingTaskView(StrictModel):
    task_id: str
    task_type: None
    status: Literal["PENDING"]
    selected_tool_run_id: None
    selected_result_id: None
    created_at: str
    updated_at: str


class MessageSubmissionData(StrictModel):
    conversation_id: str
    user_message: UserMessageView
    task: PendingTaskView
    assistant_message: None = None
    needs_input: None = None
    result_summary: None = None
    explanation: None = None
    idempotency_replayed: Literal[False] = False


class MessageSubmissionResponse(StrictModel):
    request_id: str
    data: MessageSubmissionData


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
) -> ConversationCreateResponse:
    conversation = service.create(actor_context, body.title)
    return ConversationCreateResponse(
        request_id=request.state.request_id,
        data=ConversationView(
            conversation_id=conversation.conversation_id,
            title=conversation.title,
            created_at=_utc_text(conversation.created_at),
            updated_at=_utc_text(conversation.updated_at),
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


@router.post(
    "/{conversation_id}/messages",
    response_model=MessageSubmissionResponse,
)
def submit_message(
    conversation_id: str,
    body: MessageSubmissionRequest,
    request: Request,
    actor_context: Annotated[ActorContext, Depends(get_actor_context)],
    service: Annotated[
        MessageSubmissionService,
        Depends(get_message_submission_service),
    ],
) -> MessageSubmissionResponse:
    submission = service.prepare_submission(
        actor_context,
        conversation_id=conversation_id,
        request_id=request.state.request_id,
        content_text=body.content_text,
        submission_mode=body.submission_mode,
        target_task_id=body.target_task_id,
    )
    message = submission.user_message
    task = submission.task
    return MessageSubmissionResponse(
        request_id=request.state.request_id,
        data=MessageSubmissionData(
            conversation_id=submission.conversation_id,
            user_message=UserMessageView(
                message_id=message.message_id,
                role="USER",
                content_text=message.content_text,
                created_at=_utc_text(message.created_at),
            ),
            task=PendingTaskView(
                task_id=task.task_id,
                task_type=None,
                status="PENDING",
                selected_tool_run_id=None,
                selected_result_id=None,
                created_at=_utc_text(task.created_at),
                updated_at=_utc_text(task.updated_at),
            ),
        ),
    )
