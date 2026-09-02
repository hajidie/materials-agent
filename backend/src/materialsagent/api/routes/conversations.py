from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Header, Query, Request, status
from pydantic import BaseModel, ConfigDict

from materialsagent.api.dependencies import (
    get_actor_context,
    get_chat_orchestration_service,
    get_conversation_service,
    get_message_submission_service,
    get_optional_tool_workflow_service,
)
from materialsagent.application.chat_orchestration import (
    ChatOrchestrationService,
)
from materialsagent.application.context import ActorContext
from materialsagent.application.conversations import ConversationService
from materialsagent.application.errors import (
    ApplicationInternalError,
    ApplicationValidationError,
)
from materialsagent.application.idempotency import validate_idempotency_key
from materialsagent.application.messages import MessageSubmissionService
from materialsagent.application.tool_workflow import (
    ToolWorkflowProjection,
    ToolWorkflowService,
)
from materialsagent.api.routes.tool_results import (
    ResultArtifactView,
    _public_json,
)


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


class MessageTaskView(StrictModel):
    task_id: str
    task_type: Literal["KNOWLEDGE_QA", "TOOL_EXECUTION"] | None
    status: Literal[
        "PENDING",
        "RUNNING",
        "NEEDS_INPUT",
        "READY",
        "SUCCEEDED",
        "PARTIALLY_SUCCEEDED",
        "FAILED",
    ]
    selected_tool_run_id: str | None
    selected_result_id: str | None
    created_at: str
    updated_at: str


class AssistantMessageView(StrictModel):
    message_id: str
    role: Literal["ASSISTANT"]
    content_text: str
    created_at: str


class NeedsInputView(StrictModel):
    missing_fields: list[str]
    ambiguous_fields: list[dict[str, object]]
    normalized_input: dict[str, object]


class ResultSummaryView(StrictModel):
    result_id: str
    tool_run_id: str
    status: Literal["SUCCEEDED", "PARTIALLY_SUCCEEDED", "FAILED"]
    requested_outputs: list[str]
    completed_outputs: list[str]
    failed_outputs: list[str]
    data: dict[str, object]
    artifacts: list[ResultArtifactView]
    warnings: list[object]
    provenance: dict[str, object]
    error: dict[str, object] | None


class ExplanationView(StrictModel):
    explanation_id: str
    result_id: str
    status: Literal["SUCCEEDED", "FAILED"]
    language: str
    text: str | None
    error_code: str | None
    safe_error_message: str | None
    llm_call_id: str
    llm_call_status: Literal["SUCCEEDED", "FAILED"]


class MessageSubmissionData(StrictModel):
    conversation_id: str
    user_message: UserMessageView
    task: MessageTaskView
    assistant_message: AssistantMessageView | None = None
    needs_input: NeedsInputView | None = None
    result_summary: ResultSummaryView | None = None
    explanation: ExplanationView | None = None
    latest_explanation_failure: ExplanationView | None = None
    idempotency_replayed: bool = False


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
    orchestration_service: Annotated[
        ChatOrchestrationService,
        Depends(get_chat_orchestration_service),
    ],
    tool_workflow_service: Annotated[
        ToolWorkflowService | None,
        Depends(get_optional_tool_workflow_service),
    ],
    idempotency_key: Annotated[
        str | None,
        Header(alias="Idempotency-Key"),
    ] = None,
) -> MessageSubmissionResponse:
    try:
        validated_idempotency_key = validate_idempotency_key(idempotency_key)
    except (TypeError, ValueError):
        raise ApplicationValidationError() from None
    submission = service.prepare_submission(
        actor_context,
        conversation_id=conversation_id,
        request_id=request.state.request_id,
        content_text=body.content_text,
        submission_mode=body.submission_mode,
        target_task_id=body.target_task_id,
        idempotency_key=validated_idempotency_key,
    )
    projection = (
        orchestration_service.load_current_submission(
            actor_context,
            submission,
        )
        if submission.idempotency_replayed
        else orchestration_service.orchestrate_submission(
            actor_context,
            submission,
        )
    )
    workflow: ToolWorkflowProjection | None = None
    if (
        not submission.idempotency_replayed
        and projection.task.task_type == "TOOL_EXECUTION"
        and projection.task.current_status == "READY"
    ):
        if projection.revision is None:
            raise ApplicationInternalError(task_id=projection.task.task_id)
        if tool_workflow_service is not None:
            workflow = tool_workflow_service.execute(
                actor_context,
                task_id=projection.task.task_id,
                task_input_revision_id=(
                    projection.revision.task_input_revision_id
                ),
                request_id=projection.user_message.request_id,
            )
    elif (
        submission.idempotency_replayed
        and projection.task.task_type == "TOOL_EXECUTION"
        and projection.task.selected_result_id is not None
    ):
        if tool_workflow_service is None:
            raise ApplicationInternalError(task_id=projection.task.task_id)
        workflow = tool_workflow_service.load_current_for_task(
            actor_context,
            task_id=projection.task.task_id,
        )
    message = projection.user_message
    task = workflow.task if workflow is not None else projection.task
    assistant = projection.assistant_message
    revision = projection.revision
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
            task=MessageTaskView(
                task_id=task.task_id,
                task_type=task.task_type,
                status=task.current_status,
                selected_tool_run_id=task.selected_tool_run_id,
                selected_result_id=task.selected_result_id,
                created_at=_utc_text(task.created_at),
                updated_at=_utc_text(task.updated_at),
            ),
            assistant_message=(
                None
                if assistant is None
                else AssistantMessageView(
                    message_id=assistant.message_id,
                    role="ASSISTANT",
                    content_text=assistant.content_text,
                    created_at=_utc_text(assistant.created_at),
                )
            ),
            needs_input=(
                None
                if task.current_status != "NEEDS_INPUT" or revision is None
                else NeedsInputView(
                    missing_fields=list(revision.missing_fields),
                    ambiguous_fields=list(revision.ambiguous_fields),
                    normalized_input=dict(revision.normalized_input or {}),
                )
            ),
            result_summary=(
                None
                if workflow is None
                else ResultSummaryView(
                    result_id=workflow.result.result_id,
                    tool_run_id=workflow.result.tool_run_id,
                    status=workflow.result.status,
                    requested_outputs=list(
                        workflow.result.requested_outputs
                    ),
                    completed_outputs=list(
                        workflow.result.completed_outputs
                    ),
                    failed_outputs=list(
                        workflow.result.failed_outputs
                    ),
                    data=_public_json(workflow.result.data),
                    artifacts=[
                        ResultArtifactView(
                            **{
                                field: getattr(artifact, field)
                                for field in artifact.__dataclass_fields__
                            }
                        )
                        for artifact in workflow.artifacts
                    ],
                    warnings=_public_json(workflow.result.warnings),
                    provenance=_public_json(
                        workflow.result.provenance
                    ),
                    error=(
                        None
                        if workflow.result.error is None
                        else _public_json(workflow.result.error)
                    ),
                )
            ),
            explanation=(
                None
                if workflow is None
                else ExplanationView(
                    explanation_id=(
                        workflow.explanation.explanation_id
                    ),
                    result_id=workflow.explanation.result_id,
                    status=workflow.explanation.status,
                    language=workflow.explanation.language,
                    text=workflow.explanation.text,
                    error_code=workflow.explanation.error_code,
                    safe_error_message=(
                        workflow.explanation.safe_error_message
                    ),
                    llm_call_id=(
                        workflow.explanation_call.llm_call_id
                    ),
                    llm_call_status=(
                        workflow.explanation_call.status
                    ),
                )
            ),
            latest_explanation_failure=(
                None
                if (
                    workflow is None
                    or workflow.latest_failed_explanation is None
                    or workflow.latest_failed_explanation_call is None
                )
                else ExplanationView(
                    explanation_id=(
                        workflow.latest_failed_explanation.explanation_id
                    ),
                    result_id=(
                        workflow.latest_failed_explanation.result_id
                    ),
                    status="FAILED",
                    language=(
                        workflow.latest_failed_explanation.language
                    ),
                    text=None,
                    error_code=(
                        workflow.latest_failed_explanation.error_code
                    ),
                    safe_error_message=(
                        workflow.latest_failed_explanation.safe_error_message
                    ),
                    llm_call_id=(
                        workflow.latest_failed_explanation_call.llm_call_id
                    ),
                    llm_call_status="FAILED",
                )
            ),
            idempotency_replayed=submission.idempotency_replayed,
        ),
    )
