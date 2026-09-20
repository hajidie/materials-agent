from typing import Annotated
from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field
from materialsagent.api.dependencies import get_actor_context
from materialsagent.application.context import ActorContext
from materialsagent.application.chat_artifacts import ChatArtifacts
from .agent_runs import runtime_for

router = APIRouter(prefix="/api/v1/conversations/{conversation_id}", tags=["chat"])


def service(request):
    return ChatArtifacts(runtime_for(request).store.sessions, getattr(request.app.state, "ml_resources", None))


@router.get("/result-messages")
def messages(conversation_id: str, request: Request, actor: Annotated[ActorContext, Depends(get_actor_context)]):
    return {"data": {"items": service(request).messages(actor.actor_id, conversation_id)}}


@router.get("/messages/{message_id}/artifacts/{attachment_id}")
def view(conversation_id: str, message_id: str, attachment_id: str, request: Request,
         actor: Annotated[ActorContext, Depends(get_actor_context)]):
    return {"data": service(request).view(actor.actor_id, conversation_id, message_id, attachment_id)}


class ObservationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    cursor: str | None = Field(default=None, max_length=128)


@router.post("/results/reconcile")
def observe(conversation_id: str, request: Request, actor: Annotated[ActorContext, Depends(get_actor_context)], body: ObservationRequest):
    return {"data": service(request).observe(actor.actor_id, conversation_id, body.cursor)}


@router.get("/messages/{message_id}/artifacts/{attachment_id}/download/{member}")
def download(conversation_id: str, message_id: str, attachment_id: str, member: str, request: Request,
             actor: Annotated[ActorContext, Depends(get_actor_context)]):
    from materialsagent.application.errors import ResourceNotFoundError
    import csv
    from io import StringIO
    chat = service(request)
    target = chat.artifact(actor.actor_id, conversation_id, message_id, attachment_id)
    if target["kind"] in ("ebsd_image", "image") and member == "image":
        from materialsagent.api.dependencies import get_asset_service
        value = get_asset_service(request).get_content(actor, attachment_id)
        extension = "png" if value.media_type == "image/png" else "jpg"
        return Response(value.payload, media_type=value.media_type, headers={"Content-Disposition": f'attachment; filename="image.{extension}"'})
    if target["kind"] == "prediction" and member == "predictions.csv":
        result = chat.prediction(actor.actor_id, conversation_id, attachment_id)
        output = StringIO(newline="")
        writer = csv.writer(output)
        writer.writerow(["行", "预测值", "单位"])
        for i, value in enumerate(result["values"]):
            writer.writerow([i + 1, value, result["unit"] or ""])
        return Response(output.getvalue().encode("utf-8-sig"), media_type="text/csv", headers={"Content-Disposition": 'attachment; filename="predictions.csv"'})
    if target["kind"] != "dataset" or member != "dataset.csv":
        raise ResourceNotFoundError()
    spool, media = chat.resources.download(actor.actor_id, conversation_id, attachment_id, member)
    def chunks():
        try:
            while payload := spool.read(65536):
                yield payload
        finally:
            spool.close()
    from starlette.background import BackgroundTask
    return StreamingResponse(chunks(), media_type=media, headers={"Content-Disposition": 'attachment; filename="dataset.csv"'}, background=BackgroundTask(spool.close))
