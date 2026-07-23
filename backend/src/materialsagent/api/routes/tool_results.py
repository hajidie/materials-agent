from __future__ import annotations

from datetime import datetime, timezone
from collections.abc import Mapping
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict

from materialsagent.api.dependencies import (
    get_actor_context,
    get_tool_result_query_service,
)
from materialsagent.application.context import ActorContext
from materialsagent.application.result_service import (
    ToolResultQueryService,
)


router = APIRouter(prefix="/api/v1/tool-results", tags=["tool-results"])


def _public_json(value: object) -> object:
    if isinstance(value, Mapping):
        return {key: _public_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_public_json(item) for item in value]
    return value


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


class ResultArtifactView(StrictModel):
    asset_id: str
    status: Literal["AVAILABLE"]
    role: Literal["requested_output", "intermediate", "supporting"]
    media_type: str
    width: int
    height: int
    bit_depth: int
    size_bytes: int
    sha256: str
    content_url: str


class ToolResultView(StrictModel):
    result_id: str
    task_id: str
    tool_run_id: str
    status: Literal["SUCCEEDED", "PARTIALLY_SUCCEEDED", "FAILED"]
    requested_outputs: list[str]
    completed_outputs: list[str]
    failed_outputs: list[str]
    data: dict[str, object]
    artifacts: list[ResultArtifactView]
    warnings: list[object]
    tool_id: str
    tool_version: str
    schema_version: str
    provenance: dict[str, object]
    error: dict[str, object] | None
    created_at: str


class ToolResultResponse(StrictModel):
    request_id: str
    data: ToolResultView


@router.get("/{result_id}", response_model=ToolResultResponse)
def get_tool_result(
    result_id: str,
    request: Request,
    actor: Annotated[ActorContext, Depends(get_actor_context)],
    service: Annotated[
        ToolResultQueryService,
        Depends(get_tool_result_query_service),
    ],
) -> ToolResultResponse:
    projection = service.get(actor, result_id)
    result = projection.result
    return ToolResultResponse(
        request_id=request.state.request_id,
        data=ToolResultView(
            result_id=result.result_id,
            task_id=result.task_id,
            tool_run_id=result.tool_run_id,
            status=result.status,
            requested_outputs=list(result.requested_outputs),
            completed_outputs=list(result.completed_outputs),
            failed_outputs=list(result.failed_outputs),
            data=_public_json(result.data),
            artifacts=[
                ResultArtifactView(
                    **{
                        field: getattr(artifact, field)
                        for field in artifact.__dataclass_fields__
                    }
                )
                for artifact in projection.artifacts
            ],
            warnings=_public_json(result.warnings),
            tool_id=result.tool_id,
            tool_version=result.tool_version,
            schema_version=result.schema_version,
            provenance=_public_json(result.provenance),
            error=(
                None
                if result.error is None
                else _public_json(result.error)
            ),
            created_at=_utc_text(result.created_at),
        ),
    )
