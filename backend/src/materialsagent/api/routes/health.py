from datetime import datetime, timezone
from typing import Literal

from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict


router = APIRouter(prefix="/api/v1/health", tags=["health"])


class LiveHealthResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["LIVE"] = "LIVE"
    checked_at: str
    request_id: str


class ReadyHealthResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["NOT_READY"] = "NOT_READY"
    checked_at: str
    request_id: str


def _checked_at() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


@router.get("/live", response_model=LiveHealthResponse)
def live(request: Request) -> LiveHealthResponse:
    return LiveHealthResponse(
        checked_at=_checked_at(),
        request_id=request.state.request_id,
    )


@router.get("/ready", response_model=ReadyHealthResponse, status_code=503)
def ready(request: Request) -> ReadyHealthResponse:
    return ReadyHealthResponse(
        checked_at=_checked_at(),
        request_id=request.state.request_id,
    )
