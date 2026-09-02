from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Header, Request
from pydantic import BaseModel, ConfigDict, Field

from materialsagent.api.dependencies import (
    get_actor_context,
    get_task_query_service,
    get_tool_retry_service,
)
from materialsagent.application.context import ActorContext
from materialsagent.application.tasks import TaskQueryService
from materialsagent.application.retries import (
    DEFAULT_RETRY_REASON,
    ToolRetryService,
)
from materialsagent.application.errors import ApplicationValidationError
from materialsagent.application.idempotency import validate_idempotency_key
from materialsagent.api.routes.timeline import (
    AssetSummaryView,
    ExplanationSummaryView,
    NeedsInputView,
    ToolRunSummaryView,
    _asset_view,
    _explanation_view,
    _tool_run_view,
)


router = APIRouter(prefix="/api/v1/tasks", tags=["tasks"])


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


class SelectedResultSummaryView(StrictModel):
    result_id: str
    status: Literal["SUCCEEDED", "PARTIALLY_SUCCEEDED", "FAILED"]
    requested_outputs: list[str]
    completed_outputs: list[str]
    failed_outputs: list[str]


class TaskView(StrictModel):
    task_id: str
    conversation_id: str
    task_type: Literal["KNOWLEDGE_QA", "TOOL_EXECUTION"] | None
    status: Literal[
        "PENDING",
        "READY",
        "RUNNING",
        "NEEDS_INPUT",
        "SUCCEEDED",
        "PARTIALLY_SUCCEEDED",
        "FAILED",
    ]
    anchor_at: str
    selected_tool_run_id: str | None
    selected_result_id: str | None
    created_at: str
    started_at: str | None
    updated_at: str
    completed_at: str | None
    error_code: str | None
    safe_error_message: str | None
    tool_id: str | None
    bound_tool_version: str | None
    bound_schema_hash: str | None
    needs_input: NeedsInputView | None
    tool_run_count: int
    tool_runs: list[ToolRunSummaryView]
    selected_result_summary: SelectedResultSummaryView | None
    assets: list[AssetSummaryView]
    explanation_summary: ExplanationSummaryView | None
    latest_explanation_failure: ExplanationSummaryView | None


class TaskResponse(StrictModel):
    request_id: str
    data: TaskView


class ToolRetryRequest(StrictModel):
    reason: str = Field(
        default=DEFAULT_RETRY_REASON,
        min_length=1,
        max_length=256,
    )


class RetriedToolRunView(StrictModel):
    tool_run_id: str
    attempt_no: int
    status: Literal[
        "PENDING",
        "RUNNING",
        "SUCCEEDED",
        "PARTIALLY_SUCCEEDED",
        "FAILED",
    ]
    task_input_revision_id: str


class ToolRetryData(StrictModel):
    task_id: str
    task_status: Literal[
        "PENDING",
        "RUNNING",
        "NEEDS_INPUT",
        "SUCCEEDED",
        "PARTIALLY_SUCCEEDED",
        "FAILED",
    ]
    tool_run: RetriedToolRunView
    result_id: str | None
    result_status: Literal[
        "SUCCEEDED",
        "PARTIALLY_SUCCEEDED",
        "FAILED",
    ] | None
    explanation_id: str | None
    explanation_status: Literal[
        "PENDING",
        "RUNNING",
        "SUCCEEDED",
        "FAILED",
    ] | None
    idempotency_replayed: bool


class ToolRetryResponse(StrictModel):
    request_id: str
    data: ToolRetryData


@router.get("/{task_id}", response_model=TaskResponse)
def get_task(
    task_id: str,
    request: Request,
    actor_context: Annotated[ActorContext, Depends(get_actor_context)],
    service: Annotated[TaskQueryService, Depends(get_task_query_service)],
) -> TaskResponse:
    projection = service.get_projection(actor_context, task_id)
    task = projection.task
    result = projection.selected_result
    return TaskResponse(
        request_id=request.state.request_id,
        data=TaskView(
            task_id=task.task_id,
            conversation_id=task.conversation_id,
            task_type=task.task_type,
            status=task.current_status,
            anchor_at=_utc_text(projection.anchor_at),
            selected_tool_run_id=task.selected_tool_run_id,
            selected_result_id=task.selected_result_id,
            created_at=_utc_text(task.created_at),
            started_at=_utc_text(task.started_at),
            updated_at=_utc_text(task.updated_at),
            completed_at=_utc_text(task.completed_at),
            error_code=task.error_code,
            safe_error_message=task.safe_error_message,
            tool_id=task.tool_id,
            bound_tool_version=task.bound_tool_version,
            bound_schema_hash=task.bound_schema_hash,
            needs_input=(
                None
                if projection.needs_input is None
                else NeedsInputView(
                    missing_fields=list(
                        projection.needs_input.missing_fields
                    ),
                    ambiguous_fields=list(
                        projection.needs_input.ambiguous_fields
                    ),
                    normalized_input=(
                        projection.needs_input.normalized_input
                    ),
                    candidate_tool_refs=list(
                        projection.needs_input.candidate_tool_refs
                    ),
                )
            ),
            tool_run_count=projection.tool_run_count,
            tool_runs=[
                _tool_run_view(run)
                for run in projection.tool_runs
            ],
            selected_result_summary=(
                None
                if result is None
                else SelectedResultSummaryView(
                    result_id=result.result_id,
                    status=result.status,
                    requested_outputs=list(result.requested_outputs),
                    completed_outputs=list(result.completed_outputs),
                    failed_outputs=list(result.failed_outputs),
                )
            ),
            assets=[
                _asset_view(asset) for asset in projection.assets
            ],
            explanation_summary=_explanation_view(
                projection.explanation_summary
            ),
            latest_explanation_failure=_explanation_view(
                projection.latest_explanation_failure
            ),
        ),
    )


@router.post(
    "/{task_id}/tool-runs",
    response_model=ToolRetryResponse,
)
def retry_tool_run(
    task_id: str,
    body: ToolRetryRequest,
    request: Request,
    actor_context: Annotated[ActorContext, Depends(get_actor_context)],
    service: Annotated[
        ToolRetryService,
        Depends(get_tool_retry_service),
    ],
    idempotency_key: Annotated[
        str | None,
        Header(alias="Idempotency-Key"),
    ] = None,
) -> ToolRetryResponse:
    try:
        key = validate_idempotency_key(idempotency_key)
    except (TypeError, ValueError):
        raise ApplicationValidationError(task_id=task_id) from None
    projection = service.retry(
        actor_context,
        task_id=task_id,
        request_id=request.state.request_id,
        idempotency_key=key,
        reason=body.reason,
    )
    return ToolRetryResponse(
        request_id=request.state.request_id,
        data=ToolRetryData(
            task_id=projection.task.task_id,
            task_status=projection.task.current_status,
            tool_run=RetriedToolRunView(
                tool_run_id=projection.tool_run.tool_run_id,
                attempt_no=projection.tool_run.attempt_no,
                status=projection.tool_run.current_status,
                task_input_revision_id=(
                    projection.tool_run.task_input_revision_id
                ),
            ),
            result_id=(
                None
                if projection.result is None
                else projection.result.result_id
            ),
            result_status=(
                None
                if projection.result is None
                else projection.result.status
            ),
            explanation_id=(
                None
                if projection.explanation is None
                else projection.explanation.explanation_id
            ),
            explanation_status=(
                None
                if projection.explanation is None
                else projection.explanation.status
            ),
            idempotency_replayed=projection.idempotency_replayed,
        ),
    )
