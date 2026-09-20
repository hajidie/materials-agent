"""One upload surface; uncertain requests only reconcile their original receipt."""
from hashlib import sha256
from typing import Annotated, Literal
from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from starlette.concurrency import run_in_threadpool
from materialsagent.api.dependencies import get_actor_context, get_asset_service
from materialsagent.application.context import ActorContext
from materialsagent.application.errors import ResourceNotFoundError
from materialsagent.application.ebsd_assets import upload, reconcile_upload
from .agent_runs import key_value
from .ml_resources import resources

router = APIRouter(prefix="/api/v1/conversations/{conversation_id}/attachments", tags=["attachments"])


def receipt(kind, name, identity=None):
    return {"attachment": {"attachment_id": identity, "kind": kind, "name": name} if identity else None,
            "message": "文件已上传，可以发送消息。" if identity else "上传结果尚未确认，请核查原请求。"}


@router.post("")
async def upload_attachment(conversation_id: str, request: Request,
    actor: Annotated[ActorContext, Depends(get_actor_context)],
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key")]):
    key = key_value(idempotency_key)
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > 20 * 1024**2 + 65536:
            raise HTTPException(413, detail="ATTACHMENT_TOO_LARGE")
    async def receive():
        return {"type": "http.request", "body": bytes(body), "more_body": False}
    parsed = Request(request.scope, receive)
    try:
        async with parsed.form(max_files=1, max_fields=0) as form:
            if list(form) != ["file"] or len(form.getlist("file")) != 1:
                raise ValueError()
            file = form["file"]
            name = (file.filename or "附件").replace("\\", "/").rsplit("/", 1)[-1][:200]
            payload = await file.read(20 * 1024**2 + 1)
            extension = name.rsplit(".", 1)[-1].lower()
    except (ValueError, AttributeError, TypeError):
        raise HTTPException(422, detail="INVALID_ATTACHMENT") from None
    if extension == "csv":
        if len(payload) > 20 * 1024**2:
            raise HTTPException(413, detail="ATTACHMENT_TOO_LARGE")
        service = resources(request)
        value = await run_in_threadpool(service.upload, actor.actor_id, conversation_id, key, payload, {"display_name": name})
        return {"data": receipt("dataset", name, value.get("reference_id") if value["status"] == "RESOLVED" else None)}
    if extension not in ("png", "jpg", "jpeg"):
        raise HTTPException(422, detail="UNSUPPORTED_ATTACHMENT")
    if len(payload) > 10 * 1024**2:
        raise HTTPException(413, detail="ATTACHMENT_TOO_LARGE")
    asset = await run_in_threadpool(upload, get_asset_service(request), actor, conversation_id, payload, key)
    return {"data": receipt("ebsd_image", name, asset.asset_id)}


class UploadCheck(BaseModel):
    model_config = ConfigDict(extra="forbid")
    key: str
    kind: Literal["dataset", "ebsd_image"]
    name: str = Field(min_length=1, max_length=200)


@router.post("/reconcile")
def check_attachment(conversation_id: str, body: UploadCheck, request: Request,
    actor: Annotated[ActorContext, Depends(get_actor_context)]):
    key = key_value(body.key)
    identity = None
    if body.kind == "dataset":
        service = resources(request)
        values = service.uploads(actor.actor_id, conversation_id, key=key)["items"]
        if values:
            value = service.reconcile_upload(actor.actor_id, values[0]["operation_id"])
            if value["status"] == "RESOLVED":
                identity = value.get("reference_id")
    else:
        operation = sha256((actor.actor_id + "\0" + conversation_id + "\0" + key).encode()).hexdigest()
        try:
            asset = reconcile_upload(get_asset_service(request), actor, conversation_id, "asset_" + operation[:40])
            if asset.current_status == "AVAILABLE":
                identity = asset.asset_id
        except ResourceNotFoundError:
            pass
    return {"data": receipt(body.kind, body.name, identity)}
