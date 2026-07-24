from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from materialsagent.api.dependencies import (
    get_actor_context,
    get_timeline_query_service,
)
from materialsagent.application.context import ActorContext
from materialsagent.application.timeline import (
    MessageTimelineItem,
    PublicExplanationSummary,
    PublicMessage,
    PublicResultSummary,
    PublicToolRunSummary,
    SafeTimelineError,
    TimelineQueryService,
    ToolTaskTimelineItem,
)
from materialsagent.application.result_service import (
    ResultArtifactProjection,
)


router = APIRouter(
    prefix="/api/v1/conversations",
    tags=["timeline"],
)


def _utc_text(value: datetime | None) -> str | None:
    if value is None:
        return None
    if (
        value.tzinfo is None
        or value.utcoffset() is None
        or value.utcoffset() != timezone.utc.utcoffset(value)
    ):
        raise ValueError("Public timestamps must be UTC.")
    return value.isoformat().replace("+00:00", "Z")


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TimelineConversationView(StrictModel):
    conversation_id: str
    title: str | None


class TimelineMessageView(StrictModel):
    message_id: str
    role: Literal["USER", "ASSISTANT"]
    content_text: str
    created_at: str


class SafeErrorView(StrictModel):
    code: str
    message: str


class TimelineTaskView(StrictModel):
    task_id: str
    task_type: Literal["KNOWLEDGE_QA", "TOOL_EXECUTION"] | None
    status: Literal[
        "PENDING",
        "RUNNING",
        "NEEDS_INPUT",
        "SUCCEEDED",
        "PARTIALLY_SUCCEEDED",
        "FAILED",
    ]
    selected_tool_run_id: str | None
    selected_result_id: str | None
    created_at: str
    started_at: str | None
    updated_at: str
    completed_at: str | None
    error_code: str | None
    safe_error_message: str | None


class DiagnosticSummaryView(StrictModel):
    step: str | None
    status: str | None
    duration_ms: int | None
    error_code: str | None
    safe_error_message: str | None


class ToolRunSummaryView(StrictModel):
    tool_run_id: str
    attempt_no: int
    tool_id: str
    tool_version: str
    schema_version: str
    status: Literal[
        "PENDING",
        "RUNNING",
        "SUCCEEDED",
        "PARTIALLY_SUCCEEDED",
        "FAILED",
    ]
    requested_outputs: list[str]
    completed_outputs: list[str]
    failed_outputs: list[str]
    created_at: str
    started_at: str | None
    completed_at: str | None
    duration_ms: int | None
    diagnostics_summary: list[DiagnosticSummaryView]
    error: SafeErrorView | None
    is_selected: bool


class ToolRunCollectionView(StrictModel):
    attempt_count: int
    selected_tool_run: ToolRunSummaryView | None
    has_history: bool


class ResultSummaryView(StrictModel):
    result_id: str
    tool_run_id: str
    status: Literal["SUCCEEDED", "PARTIALLY_SUCCEEDED", "FAILED"]
    requested_outputs: list[str]
    completed_outputs: list[str]
    failed_outputs: list[str]
    data: dict[str, object]
    warnings: list[object]
    provenance: dict[str, object]
    error: dict[str, object] | None
    tool_id: str
    tool_version: str
    schema_version: str
    created_at: str


class AssetSummaryView(StrictModel):
    asset_id: str
    status: Literal["AVAILABLE"]
    role: str
    media_type: str
    width: int
    height: int
    bit_depth: int
    size_bytes: int
    sha256: str
    content_url: str


class ExplanationSummaryView(StrictModel):
    explanation_id: str
    attempt_no: int
    status: Literal["SUCCEEDED", "FAILED"]
    language: str
    text: str | None
    completed_at: str | None
    duration_ms: int | None
    error_code: str | None
    safe_error_message: str | None


class NeedsInputView(StrictModel):
    missing_fields: list[str]
    ambiguous_fields: list[dict[str, object]]
    normalized_input: dict[str, object] | None


class UserMessageTimelineItemView(StrictModel):
    item_type: Literal["USER_MESSAGE"]
    item_id: str
    task_id: str
    anchor_at: str
    message: TimelineMessageView


class AssistantMessageTimelineItemView(StrictModel):
    item_type: Literal["ASSISTANT_MESSAGE"]
    item_id: str
    task_id: str
    anchor_at: str
    message: TimelineMessageView


class ToolTaskTimelineItemView(StrictModel):
    item_type: Literal["TOOL_TASK"]
    item_id: str
    task_id: str
    anchor_at: str
    initial_user_message: TimelineMessageView | None
    task: TimelineTaskView
    input_thread: list[TimelineMessageView]
    input_thread_count: int
    input_thread_truncated: bool
    tool_runs: ToolRunCollectionView
    result: ResultSummaryView | None
    assets: list[AssetSummaryView]
    explanation: ExplanationSummaryView | None
    latest_explanation_failure: ExplanationSummaryView | None
    needs_input: NeedsInputView | None
    errors: list[SafeErrorView]


TimelineItemView = Annotated[
    (
        UserMessageTimelineItemView
        | AssistantMessageTimelineItemView
        | ToolTaskTimelineItemView
    ),
    Field(discriminator="item_type"),
]


class TimelineDataView(StrictModel):
    conversation: TimelineConversationView
    items: list[TimelineItemView]
    next_cursor: str | None
    has_more: bool


class TimelineResponse(StrictModel):
    request_id: str
    data: TimelineDataView


def _message_view(message: PublicMessage) -> TimelineMessageView:
    return TimelineMessageView(
        message_id=message.message_id,
        role=message.role,
        content_text=message.content_text,
        created_at=_utc_text(message.created_at),
    )


def _error_view(error: SafeTimelineError | None) -> SafeErrorView | None:
    if error is None:
        return None
    return SafeErrorView(code=error.code, message=error.message)


def _tool_run_view(
    run: PublicToolRunSummary | None,
) -> ToolRunSummaryView | None:
    if run is None:
        return None
    return ToolRunSummaryView(
        tool_run_id=run.tool_run_id,
        attempt_no=run.attempt_no,
        tool_id=run.tool_id,
        tool_version=run.tool_version,
        schema_version=run.schema_version,
        status=run.status,
        requested_outputs=list(run.requested_outputs),
        completed_outputs=list(run.completed_outputs),
        failed_outputs=list(run.failed_outputs),
        created_at=_utc_text(run.created_at),
        started_at=_utc_text(run.started_at),
        completed_at=_utc_text(run.completed_at),
        duration_ms=run.duration_ms,
        diagnostics_summary=[
            DiagnosticSummaryView(**item)
            for item in run.diagnostics_summary
        ],
        error=_error_view(run.error),
        is_selected=run.is_selected,
    )


def _result_view(
    result: PublicResultSummary | None,
) -> ResultSummaryView | None:
    if result is None:
        return None
    return ResultSummaryView(
        result_id=result.result_id,
        tool_run_id=result.tool_run_id,
        status=result.status,
        requested_outputs=list(result.requested_outputs),
        completed_outputs=list(result.completed_outputs),
        failed_outputs=list(result.failed_outputs),
        data=result.data,
        warnings=list(result.warnings),
        provenance=result.provenance,
        error=result.error,
        tool_id=result.tool_id,
        tool_version=result.tool_version,
        schema_version=result.schema_version,
        created_at=_utc_text(result.created_at),
    )


def _asset_view(asset: ResultArtifactProjection) -> AssetSummaryView:
    return AssetSummaryView(
        asset_id=asset.asset_id,
        status="AVAILABLE",
        role=asset.role,
        media_type=asset.media_type,
        width=asset.width,
        height=asset.height,
        bit_depth=asset.bit_depth,
        size_bytes=asset.size_bytes,
        sha256=asset.sha256,
        content_url=asset.content_url,
    )


def _explanation_view(
    explanation: PublicExplanationSummary | None,
) -> ExplanationSummaryView | None:
    if explanation is None:
        return None
    return ExplanationSummaryView(
        explanation_id=explanation.explanation_id,
        attempt_no=explanation.attempt_no,
        status=explanation.status,
        language=explanation.language,
        text=explanation.text,
        completed_at=_utc_text(explanation.completed_at),
        duration_ms=explanation.duration_ms,
        error_code=explanation.error_code,
        safe_error_message=explanation.safe_error_message,
    )


def _item_view(
    item: MessageTimelineItem | ToolTaskTimelineItem,
) -> TimelineItemView:
    if isinstance(item, MessageTimelineItem):
        values = {
            "item_type": item.item_type,
            "item_id": item.item_id,
            "task_id": item.task_id,
            "anchor_at": _utc_text(item.anchor_at),
            "message": _message_view(item.message),
        }
        if item.item_type == "USER_MESSAGE":
            return UserMessageTimelineItemView(**values)
        return AssistantMessageTimelineItemView(**values)
    task = item.task
    return ToolTaskTimelineItemView(
        item_type="TOOL_TASK",
        item_id=item.item_id,
        task_id=item.task_id,
        anchor_at=_utc_text(item.anchor_at),
        initial_user_message=(
            None
            if item.initial_user_message is None
            else _message_view(item.initial_user_message)
        ),
        task=TimelineTaskView(
            task_id=task.task_id,
            task_type=task.task_type,
            status=task.status,
            selected_tool_run_id=task.selected_tool_run_id,
            selected_result_id=task.selected_result_id,
            created_at=_utc_text(task.created_at),
            started_at=_utc_text(task.started_at),
            updated_at=_utc_text(task.updated_at),
            completed_at=_utc_text(task.completed_at),
            error_code=task.error_code,
            safe_error_message=task.safe_error_message,
        ),
        input_thread=[
            _message_view(message) for message in item.input_thread
        ],
        input_thread_count=item.input_thread_count,
        input_thread_truncated=item.input_thread_truncated,
        tool_runs=ToolRunCollectionView(
            attempt_count=item.tool_runs.attempt_count,
            selected_tool_run=_tool_run_view(
                item.tool_runs.selected_tool_run
            ),
            has_history=item.tool_runs.has_history,
        ),
        result=_result_view(item.result),
        assets=[_asset_view(asset) for asset in item.assets],
        explanation=_explanation_view(item.explanation),
        latest_explanation_failure=_explanation_view(
            item.latest_explanation_failure
        ),
        needs_input=(
            None
            if item.needs_input is None
            else NeedsInputView(
                missing_fields=list(item.needs_input.missing_fields),
                ambiguous_fields=list(
                    item.needs_input.ambiguous_fields
                ),
                normalized_input=item.needs_input.normalized_input,
            )
        ),
        errors=[
            SafeErrorView(code=error.code, message=error.message)
            for error in item.errors
        ],
    )


@router.get(
    "/{conversation_id}/timeline",
    response_model=TimelineResponse,
)
def get_timeline(
    conversation_id: str,
    request: Request,
    actor_context: Annotated[ActorContext, Depends(get_actor_context)],
    service: Annotated[
        TimelineQueryService,
        Depends(get_timeline_query_service),
    ],
    limit: Annotated[int, Query(ge=1, le=50)] = 20,
    cursor: str | None = None,
) -> TimelineResponse:
    page = service.get_page(
        actor_context,
        conversation_id=conversation_id,
        limit=limit,
        cursor=cursor,
    )
    return TimelineResponse(
        request_id=request.state.request_id,
        data=TimelineDataView(
            conversation=TimelineConversationView(
                conversation_id=page.conversation.conversation_id,
                title=page.conversation.title,
            ),
            items=[_item_view(item) for item in page.items],
            next_cursor=page.next_cursor,
            has_more=page.has_more,
        ),
    )
