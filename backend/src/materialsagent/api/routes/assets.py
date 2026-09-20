from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query, Request, Response, Header, HTTPException
from pydantic import BaseModel, ConfigDict

from materialsagent.api.dependencies import (
    get_actor_context,
    get_asset_service,
)
from materialsagent.application.asset_service import AssetService
from materialsagent.application.context import ActorContext
from materialsagent.domain.models.asset import Asset


router = APIRouter(tags=["assets"])


class AssetResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

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


def project_asset(asset: Asset) -> dict[str, object]:
    return {
        "asset_id": asset.asset_id,
        "status": asset.current_status,
        "asset_type": asset.asset_type,
        "source_type": asset.source_type,
        "producer_tool_run_id": asset.producer_tool_run_id,
        "role": asset.role,
        "media_type": asset.media_type,
        "width": asset.width,
        "height": asset.height,
        "bit_depth": asset.bit_depth,
        "size_bytes": asset.size_bytes,
        "sha256": asset.sha256,
        "created_at": _utc_text(asset.created_at),
        "available_at": _utc_text(asset.available_at),
        "error_code": asset.error_code,
        "safe_error_message": asset.safe_error_message,
    }


@router.get("/api/v1/assets/{asset_id}", response_model=AssetResponse)
def get_asset(
    asset_id: str,
    request: Request,
    actor_context: Annotated[ActorContext, Depends(get_actor_context)],
    service: Annotated[AssetService, Depends(get_asset_service)],
) -> AssetResponse:
    asset = service.get(actor_context, asset_id)
    return AssetResponse(
        request_id=request.state.request_id,
        data=project_asset(asset),
    )


@router.get("/api/v1/assets/{asset_id}/content")
def get_asset_content(
    asset_id: str,
    actor_context: Annotated[ActorContext, Depends(get_actor_context)],
    service: Annotated[AssetService, Depends(get_asset_service)],
    disposition: Annotated[
        Literal["inline", "attachment"],
        Query(),
    ] = "inline",
) -> Response:
    content = service.get_content(actor_context, asset_id)
    return Response(
        content=content.payload,
        media_type=content.media_type,
        headers={
            "Content-Length": str(len(content.payload)),
            "Content-Disposition": (
                f'{disposition}; filename="{content.filename}"'
            ),
            "X-Content-Type-Options": "nosniff",
        },
    )
