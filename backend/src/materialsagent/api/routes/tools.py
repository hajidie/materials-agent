from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request, status
from pydantic import BaseModel, ConfigDict

from materialsagent.api.dependencies import (
    get_actor_context,
    get_asset_service,
    get_tool_catalog_service,
    get_tool_execution_service,
    get_tool_run_query_service,
    require_m5_dev_routes,
)
from materialsagent.application.context import ActorContext
from materialsagent.application.asset_service import AssetService
from materialsagent.application.errors import ResourceNotFoundError
from materialsagent.application.tool_execution import (
    ToolExecutionService,
    ToolRunQueryService,
)
from materialsagent.application.tools import ToolCatalogService, UnknownToolError
from materialsagent.domain.models.tool_run import ToolRun


router = APIRouter(tags=["tools"])


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CatalogResponse(StrictModel):
    request_id: str
    data: Any


class ExecuteToolRequest(StrictModel):
    task_input_revision_id: str


class ToolRunResponse(StrictModel):
    request_id: str
    data: dict[str, object]


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


def _project_tool_run(
    tool_run: ToolRun,
    *,
    asset_ids: list[str] | None = None,
) -> dict[str, object]:
    return {
        "tool_run_id": tool_run.tool_run_id,
        "task_id": tool_run.task_id,
        "task_input_revision_id": tool_run.task_input_revision_id,
        "attempt_no": tool_run.attempt_no,
        "tool_id": tool_run.tool_id,
        "tool_version": tool_run.tool_version,
        "schema_hash": tool_run.schema_hash,
        "requested_outputs": list(tool_run.requested_outputs),
        "completed_outputs": list(tool_run.completed_outputs),
        "failed_outputs": list(tool_run.failed_outputs),
        "execution_input": dict(tool_run.execution_input),
        "actual_runtime_parameters": (
            dict(tool_run.actual_runtime_parameters)
            if tool_run.actual_runtime_parameters is not None
            else None
        ),
        "diagnostics": [dict(item) for item in tool_run.diagnostics],
        "output_summary": (
            dict(tool_run.output_summary)
            if tool_run.output_summary is not None
            else None
        ),
        "status": tool_run.current_status,
        "model_bundle_id": tool_run.model_bundle_id,
        "created_at": _utc_text(tool_run.created_at),
        "started_at": _utc_text(tool_run.started_at),
        "completed_at": _utc_text(tool_run.completed_at),
        "duration_ms": tool_run.duration_ms,
        "error_code": tool_run.error_code,
        "safe_error_message": tool_run.safe_error_message,
        "asset_ids": list(asset_ids or []),
    }


@router.get("/api/v1/tools", response_model=CatalogResponse)
def list_tools(
    request: Request,
    service: Annotated[ToolCatalogService, Depends(get_tool_catalog_service)],
) -> CatalogResponse:
    return CatalogResponse(
        request_id=request.state.request_id,
        data=service.list_entries(),
    )


@router.get("/api/v1/tools/{tool_id}", response_model=CatalogResponse)
def get_tool(
    tool_id: str,
    request: Request,
    service: Annotated[ToolCatalogService, Depends(get_tool_catalog_service)],
) -> CatalogResponse:
    try:
        entry = service.get_entry(tool_id)
    except UnknownToolError:
        raise ResourceNotFoundError() from None
    return CatalogResponse(request_id=request.state.request_id, data=entry)


@router.post(
    "/api/v1/dev/tasks/{task_id}/tool-runs",
    response_model=ToolRunResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_m5_dev_routes)],
)
def execute_tool_revision(
    task_id: str,
    payload: ExecuteToolRequest,
    request: Request,
    actor_context: Annotated[ActorContext, Depends(get_actor_context)],
    service: Annotated[ToolExecutionService, Depends(get_tool_execution_service)],
    asset_service: Annotated[AssetService, Depends(get_asset_service)],
) -> ToolRunResponse:
    receipt = service.execute_revision_with_output(
        actor_context,
        task_id=task_id,
        task_input_revision_id=payload.task_input_revision_id,
        request_id=request.state.request_id,
    )
    assets = []
    if receipt.output.images:
        assets = asset_service.create_from_output(
            actor_context,
            task_id=task_id,
            tool_run_id=receipt.tool_run.tool_run_id,
            output=receipt.output,
        )
    return ToolRunResponse(
        request_id=request.state.request_id,
        data=_project_tool_run(
            receipt.tool_run,
            asset_ids=[asset.asset_id for asset in assets],
        ),
    )


@router.get(
    "/api/v1/dev/tool-runs/{tool_run_id}",
    response_model=ToolRunResponse,
    dependencies=[Depends(require_m5_dev_routes)],
)
def get_tool_run(
    tool_run_id: str,
    request: Request,
    actor_context: Annotated[ActorContext, Depends(get_actor_context)],
    service: Annotated[ToolRunQueryService, Depends(get_tool_run_query_service)],
) -> ToolRunResponse:
    tool_run, asset_ids = service.get_with_asset_ids(
        actor_context,
        tool_run_id,
    )
    return ToolRunResponse(
        request_id=request.state.request_id,
        data=_project_tool_run(
            tool_run,
            asset_ids=asset_ids,
        ),
    )
