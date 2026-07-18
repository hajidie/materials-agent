from datetime import datetime, timezone
from typing import Literal

from fastapi import APIRouter, Request, Response
from pydantic import BaseModel, ConfigDict


router = APIRouter(prefix="/api/v1/health", tags=["health"])


class LiveHealthResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["LIVE"] = "LIVE"
    checked_at: str
    request_id: str


class ReadyHealthResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["READY", "DEGRADED", "NOT_READY"]
    components: list["ReadyComponentResponse"]
    checked_at: str
    request_id: str


class ReadyComponentResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: Literal["postgresql", "object_storage"]
    status: Literal["AVAILABLE", "UNAVAILABLE"]


def _checked_at() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


@router.get("/live", response_model=LiveHealthResponse)
def live(request: Request) -> LiveHealthResponse:
    return LiveHealthResponse(
        checked_at=_checked_at(),
        request_id=request.state.request_id,
    )


@router.get("/ready", response_model=ReadyHealthResponse)
def ready(request: Request, response: Response) -> ReadyHealthResponse:
    snapshot = request.app.state.readiness_service.check()
    response.status_code = snapshot.http_status
    return ReadyHealthResponse(
        status=snapshot.status,
        components=[
            ReadyComponentResponse(
                name=component.name,
                status=component.status,
            )
            for component in snapshot.components
        ],
        checked_at=_checked_at(),
        request_id=request.state.request_id,
    )
