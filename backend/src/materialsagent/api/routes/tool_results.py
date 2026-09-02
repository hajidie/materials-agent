from __future__ import annotations

from datetime import datetime, timezone
from collections.abc import Mapping
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Header, Request
from pydantic import BaseModel, ConfigDict, Field

from materialsagent.api.dependencies import (
    get_actor_context,
    get_tool_result_query_service,
    get_explanation_retry_service,
)
from materialsagent.application.context import ActorContext
from materialsagent.application.result_service import (
    ToolResultQueryService,
)
from materialsagent.application.retries import (
    DEFAULT_RETRY_REASON,
    ExplanationRetryService,
)
from materialsagent.application.errors import ApplicationValidationError
from materialsagent.application.idempotency import validate_idempotency_key


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
    schema_hash: str
    provenance: dict[str, object]
    error: dict[str, object] | None
    created_at: str


class ToolResultResponse(StrictModel):
    request_id: str
    data: ToolResultView


class ExplanationRetryRequest(StrictModel):
    language: str = Field(default="zh-CN", min_length=1, max_length=32)
    reason: str = Field(
        default=DEFAULT_RETRY_REASON,
        min_length=1,
        max_length=256,
    )


class ExplanationRetryView(StrictModel):
    explanation_id: str
    result_id: str
    attempt_no: int
    status: Literal["PENDING", "RUNNING", "SUCCEEDED", "FAILED"]
    language: str
    text: str | None
    error_code: str | None
    safe_error_message: str | None
    llm_call_id: str
    llm_call_status: Literal["PENDING", "RUNNING", "SUCCEEDED", "FAILED"]


class ExplanationRetryData(StrictModel):
    task_id: str
    task_status: Literal[
        "PENDING",
        "RUNNING",
        "NEEDS_INPUT",
        "SUCCEEDED",
        "PARTIALLY_SUCCEEDED",
        "FAILED",
    ]
    explanation: ExplanationRetryView
    idempotency_replayed: bool


class ExplanationRetryResponse(StrictModel):
    request_id: str
    data: ExplanationRetryData


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
            schema_hash=result.schema_hash,
            provenance=_public_json(result.provenance),
            error=(
                None
                if result.error is None
                else _public_json(result.error)
            ),
            created_at=_utc_text(result.created_at),
        ),
    )


@router.post(
    "/{result_id}/explanations",
    response_model=ExplanationRetryResponse,
)
def retry_explanation(
    result_id: str,
    body: ExplanationRetryRequest,
    request: Request,
    actor: Annotated[ActorContext, Depends(get_actor_context)],
    service: Annotated[
        ExplanationRetryService,
        Depends(get_explanation_retry_service),
    ],
    idempotency_key: Annotated[
        str | None,
        Header(alias="Idempotency-Key"),
    ] = None,
) -> ExplanationRetryResponse:
    try:
        key = validate_idempotency_key(idempotency_key)
    except (TypeError, ValueError):
        raise ApplicationValidationError() from None
    projection = service.retry(
        actor,
        result_id=result_id,
        request_id=request.state.request_id,
        idempotency_key=key,
        language=body.language,
        reason=body.reason,
    )
    explanation = projection.explanation
    call = projection.explanation_call
    return ExplanationRetryResponse(
        request_id=request.state.request_id,
        data=ExplanationRetryData(
            task_id=projection.task.task_id,
            task_status=projection.task.current_status,
            explanation=ExplanationRetryView(
                explanation_id=explanation.explanation_id,
                result_id=explanation.result_id,
                attempt_no=explanation.attempt_no,
                status=explanation.status,
                language=explanation.language,
                text=explanation.text,
                error_code=explanation.error_code,
                safe_error_message=explanation.safe_error_message,
                llm_call_id=call.llm_call_id,
                llm_call_status=call.status,
            ),
            idempotency_replayed=projection.idempotency_replayed,
        ),
    )
