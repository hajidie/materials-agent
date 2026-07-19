from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict

from materialsagent.api.dependencies import (
    get_actor_context,
    get_task_query_service,
)
from materialsagent.application.context import ActorContext
from materialsagent.application.tasks import TaskQueryService


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


class TaskView(StrictModel):
    task_id: str
    conversation_id: str
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


class TaskResponse(StrictModel):
    request_id: str
    data: TaskView


@router.get("/{task_id}", response_model=TaskResponse)
def get_task(
    task_id: str,
    request: Request,
    actor_context: Annotated[ActorContext, Depends(get_actor_context)],
    service: Annotated[TaskQueryService, Depends(get_task_query_service)],
) -> TaskResponse:
    task = service.get(actor_context, task_id)
    return TaskResponse(
        request_id=request.state.request_id,
        data=TaskView(
            task_id=task.task_id,
            conversation_id=task.conversation_id,
            task_type=task.task_type,
            status=task.current_status,
            selected_tool_run_id=task.selected_tool_run_id,
            selected_result_id=task.selected_result_id,
            created_at=_utc_text(task.created_at),
            started_at=_utc_text(task.started_at),
            updated_at=_utc_text(task.updated_at),
            completed_at=_utc_text(task.completed_at),
            error_code=task.error_code,
            safe_error_message=task.safe_error_message,
        ),
    )
