"""Owned resource operations, without Agent-budget or ML domain persistence access."""
import json
from typing import Annotated, Literal
from fastapi import APIRouter, Depends, Request, Header, Query
from fastapi.responses import StreamingResponse, JSONResponse
from pydantic import BaseModel, ConfigDict
from starlette.concurrency import run_in_threadpool
from starlette.background import BackgroundTask

from materialsagent.api.dependencies import get_actor_context
from materialsagent.application.context import ActorContext
from materialsagent.application.errors import ResourceNotFoundError, ApplicationValidationError

router = APIRouter(prefix="/api/v1")
PREFIX = "/conversations/{conversation_id}/ml"


def local_resources(request):
    # Local coordination remains readable when feature switches are turned off.
    service = getattr(request.app.state, "ml_resources", None)
    if service is None:
        raise ResourceNotFoundError()
    return service


@router.get("/capabilities")
def capabilities(request: Request, actor: Annotated[ActorContext, Depends(get_actor_context)]):
    return {"data": {"ml_resources": bool(getattr(request.app.state, "enable_materials_ml_resources", False)),
        "ml_context": bool(getattr(request.app.state, "enable_materials_ml_resource_context", False)),
        "ml_tools": bool(getattr(request.app.state, "enable_dev_materials_ml_tools", False)),
        "coordination": getattr(request.app.state, "ml_resources", None) is not None,
        "model_package": False}}


@router.get(PREFIX + "/ui-state")
def ui_state(conversation_id: str, request: Request, actor: Annotated[ActorContext, Depends(get_actor_context)],
             ):
    return {"data": local_resources(request).ui_state(actor.actor_id, conversation_id)}


@router.get(PREFIX + "/uploads")
def uploads(conversation_id: str, request: Request, actor: Annotated[ActorContext, Depends(get_actor_context)],
            limit: Annotated[int, Query(ge=1, le=100)] = 20, after: str | None = None,
            idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None):
    return {"data": local_resources(request).uploads(actor.actor_id, conversation_id,
        limit=limit, after=after, key=idempotency_key)}


def resources(request):
    if not request.app.state.enable_materials_ml_resources:
        raise ResourceNotFoundError()
    result = getattr(request.app.state, "ml_resources", None)
    if result is None:
        raise ResourceNotFoundError()
    return result


class Registration(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    resource_type: str
    resource_id: str


@router.post(PREFIX + "/datasets")
async def upload(conversation_id: str, request: Request, actor: Annotated[ActorContext, Depends(get_actor_context)],
                 idempotency_key: Annotated[str, Header(alias="Idempotency-Key")]):
    service = resources(request)
    # Bound the whole wire body, including chunked multipart overhead, before parsing.
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > 20 * 1024**2 + 65536:
            raise ApplicationValidationError(code="ML_UPLOAD_TOO_LARGE", status_code=413)
    delivered = False
    async def receive():
        nonlocal delivered
        if delivered:
            return {"type": "http.request", "body": b"", "more_body": False}
        delivered = True
        return {"type": "http.request", "body": bytes(body), "more_body": False}
    parsed = Request(request.scope, receive)
    try:
        async with parsed.form(max_files=1, max_fields=1) as form:
            if set(form) - {"file", "metadata"} or "file" not in form:
                raise ValueError()
            metadata = json.loads(form.get("metadata", "{}"))
            if not isinstance(metadata, dict) or set(metadata) - {"units", "display_name"}:
                raise ValueError()
            json.dumps(metadata, allow_nan=False)
            payload = await form["file"].read(20 * 1024**2 + 1)
            if len(payload) > 20 * 1024**2:
                raise ValueError()
    except (ValueError, AttributeError, TypeError):
        raise ApplicationValidationError(code="INVALID_ML_UPLOAD") from None
    value = await run_in_threadpool(service.upload, actor.actor_id, conversation_id, idempotency_key, payload, metadata)
    return JSONResponse({"data": {k: v for k, v in value.items() if k != "service"}},
                        200 if value["status"] == "RESOLVED" else 202)


@router.post(PREFIX + "/resources")
def register(conversation_id: str, body: Registration, request: Request, actor: Annotated[ActorContext, Depends(get_actor_context)]):
    return {"data": resources(request).register(actor.actor_id, conversation_id, body.resource_type, body.resource_id)}


@router.get(PREFIX + "/resources")
def list_refs(conversation_id: str, request: Request, actor: Annotated[ActorContext, Depends(get_actor_context)],
              limit: Annotated[int, Query(ge=1, le=100)] = 20, after: str | None = None,
              resource_type: Literal["dataset", "training_run", "model", "prediction"] | None = None, resource_id: str | None = None):
    return {"data": resources(request).list(actor.actor_id, conversation_id, limit=limit, after=after, resource_type=resource_type, resource_id=resource_id)}


@router.post(PREFIX + "/resources/{reference_id}/discover-model")
def discover_model(conversation_id: str, reference_id: str, request: Request, actor: Annotated[ActorContext, Depends(get_actor_context)]):
    from materialsagent.application.ml_resource_context import reads
    with reads(10):
        return {"data": resources(request).discover_model(actor.actor_id, conversation_id, reference_id)}


@router.get(PREFIX + "/resources/{reference_id}")
def ref(conversation_id: str, reference_id: str, request: Request, actor: Annotated[ActorContext, Depends(get_actor_context)]):
    return {"data": resources(request).reference(actor.actor_id, conversation_id, reference_id)}


@router.get(PREFIX + "/resources/{reference_id}/remote")
def remote(conversation_id: str, reference_id: str, request: Request, actor: Annotated[ActorContext, Depends(get_actor_context)]):
    from materialsagent.application.ml_resource_context import reads
    with reads(10):
        return {"data": resources(request).resource(actor.actor_id, conversation_id, reference_id)}


@router.get(PREFIX + "/resources/{reference_id}/files/{member}")
def download(conversation_id: str, reference_id: str, member: str, request: Request, actor: Annotated[ActorContext, Depends(get_actor_context)]):
    spool, media = resources(request).download(actor.actor_id, conversation_id, reference_id, member)
    def chunks():
        try:
            while data := spool.read(65536):
                yield data
        finally:
            spool.close()
    return StreamingResponse(chunks(), media_type=media, headers={"Content-Disposition": f'attachment; filename="{member}"'},
                             background=BackgroundTask(spool.close))


@router.post(PREFIX + "/resources/{reference_id}/cancel")
def cancel(conversation_id: str, reference_id: str, request: Request, actor: Annotated[ActorContext, Depends(get_actor_context)]):
    return {"data": resources(request).resource(actor.actor_id, conversation_id, reference_id, "cancel")}


@router.delete(PREFIX + "/resources/{reference_id}")
def delete_dataset(conversation_id: str, reference_id: str, request: Request, actor: Annotated[ActorContext, Depends(get_actor_context)]):
    return {"data": resources(request).resource(actor.actor_id, conversation_id, reference_id, "delete")}


@router.get(PREFIX + "/uploads/{operation_id}")
@router.post(PREFIX + "/uploads/{operation_id}/reconcile")
def upload_operation(conversation_id: str, operation_id: str, request: Request, actor: Annotated[ActorContext, Depends(get_actor_context)]):
    service = local_resources(request)
    value = service.upload_operation(actor.actor_id, operation_id)
    if value["conversation_id"] != conversation_id:
        raise ResourceNotFoundError()
    value = service.reconcile_upload(actor.actor_id, operation_id) if request.method == "POST" else value
    return {"data": {k: v for k, v in value.items() if k != "service"}}


@router.get("/conversation-deletions/{operation_id}")
@router.post("/conversation-deletions/{operation_id}/reconcile")
def deletion(operation_id: str, request: Request, actor: Annotated[ActorContext, Depends(get_actor_context)]):
    service = getattr(request.app.state, "ml_resources", None)
    if service is None:
        raise ResourceNotFoundError()
    if request.method == "POST":
        value = request.app.state.ml_deletion_coordinator.reconcile(actor, operation_id)
    else:
        value = service.repo.operation(actor.actor_id, operation_id, "deletion")["document"]
    # Internal service location fingerprints are not part of public receipts.
    return {"data": {k: v for k, v in value.items() if k not in ("service", "fence_version")}}
