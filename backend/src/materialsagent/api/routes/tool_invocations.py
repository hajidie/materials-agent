from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict

from materialsagent.api.dependencies import get_actor_context, get_invocation_service
from materialsagent.application.context import ActorContext
from materialsagent.application.tool_invocations import InvocationService


router = APIRouter(tags=["tool-invocations"])


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class InvocationToolView(StrictModel):
    tool_id: str
    version: str
    display_name: str
    execution_profile: str
    confirmation_required: bool
    confirmation_prompt: str | None


class InvocationResultView(StrictModel):
    data: dict[str, object]
    presentation: dict[str, object]


class InvocationDataView(StrictModel):
    invocation_run_id: str
    conversation_id: str
    source_message_id: str
    task_id: str | None
    status: str
    tool: InvocationToolView
    confirmation_required: bool
    confirmation_expires_at: str | None
    confirmed_at: str | None
    rejected_at: str | None
    expired_at: str | None
    dispatch_started_at: str | None
    error_code: str | None
    safe_error_message: str | None
    result: InvocationResultView | None
    created_at: str
    updated_at: str
    completed_at: str | None


class InvocationResponse(StrictModel):
    request_id: str
    data: InvocationDataView


class InvocationListResponse(StrictModel):
    request_id: str
    data: list[InvocationDataView]


@router.get("/api/v1/tool-invocations/{invocation_run_id}", response_model=InvocationResponse)
def get_invocation(
    invocation_run_id: str,
    request: Request,
    actor: Annotated[ActorContext, Depends(get_actor_context)],
    service: Annotated[InvocationService, Depends(get_invocation_service)],
) -> InvocationResponse:
    return InvocationResponse(
        request_id=request.state.request_id,
        data=InvocationDataView.model_validate(
            service.get(actor, invocation_run_id).to_dict()
        ),
    )


@router.get(
    "/api/v1/conversations/{conversation_id}/tool-invocations",
    response_model=InvocationListResponse,
)
def list_invocations(
    conversation_id: str,
    request: Request,
    actor: Annotated[ActorContext, Depends(get_actor_context)],
    service: Annotated[InvocationService, Depends(get_invocation_service)],
) -> InvocationListResponse:
    return InvocationListResponse(
        request_id=request.state.request_id,
        data=[
            InvocationDataView.model_validate(item.to_dict())
            for item in service.list_for_conversation(actor, conversation_id)
        ],
    )


@router.post(
    "/api/v1/tool-invocations/{invocation_run_id}/confirm",
    response_model=InvocationResponse,
)
def confirm_invocation(
    invocation_run_id: str,
    request: Request,
    actor: Annotated[ActorContext, Depends(get_actor_context)],
    service: Annotated[InvocationService, Depends(get_invocation_service)],
) -> InvocationResponse:
    return InvocationResponse(
        request_id=request.state.request_id,
        data=InvocationDataView.model_validate(
            service.confirm(actor, invocation_run_id).to_dict()
        ),
    )


@router.post(
    "/api/v1/tool-invocations/{invocation_run_id}/reject",
    response_model=InvocationResponse,
)
def reject_invocation(
    invocation_run_id: str,
    request: Request,
    actor: Annotated[ActorContext, Depends(get_actor_context)],
    service: Annotated[InvocationService, Depends(get_invocation_service)],
) -> InvocationResponse:
    return InvocationResponse(
        request_id=request.state.request_id,
        data=InvocationDataView.model_validate(
            service.reject(actor, invocation_run_id).to_dict()
        ),
    )
