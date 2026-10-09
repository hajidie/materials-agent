"""Closed TC4 item and receipt contracts. Fractions always refer to the whole image."""
from datetime import datetime
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator

TOOL_ID = "tc4_primary_alpha_segmentation"
MODEL_VERSION = "tc4-primary-alpha-resnet50-unet-v1"
MOCK_MODEL_VERSION = "mock-tc4-bundle"
PREPROCESSING_VERSION = "rgb-letterbox512-softmax-resize-argmax-v1"
OUTPUTS = ("overlay", "mask", "area_fraction")


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class PixelStatistics(Contract):
    width: int = Field(ge=128, le=4096)
    height: int = Field(ge=128, le=4096)
    foreground_pixels: int = Field(ge=0)
    total_pixels: int = Field(gt=0)
    area_fraction: float = Field(ge=0, le=1, allow_inf_nan=False)

    @model_validator(mode="after")
    def consistent(self):
        if (self.total_pixels != self.width * self.height or self.foreground_pixels > self.total_pixels
                or self.area_fraction != self.foreground_pixels / self.total_pixels):
            raise ValueError("Inconsistent pixel statistics.")
        return self


class ItemError(Contract):
    code: str = Field(pattern=r"^[A-Z0-9_]{1,64}$")
    message: str = Field(min_length=1, max_length=256)


class ToolRunItem(Contract):
    item_id: str
    tool_run_id: str
    actor_id: str
    ordinal: int = Field(ge=0, le=9)
    input_asset_id: str
    input_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    name: str = Field(min_length=1, max_length=200)
    request_id: str
    status: Literal["NOT_DISPATCHED", "DISPATCHED", "RESULT_RECEIVED", "SUCCEEDED", "FAILED", "OUTCOME_UNKNOWN", "SKIPPED"] = "NOT_DISPATCHED"
    receipt: dict | None = None
    statistics: PixelStatistics | None = None
    artifacts: dict[str, str] = Field(default_factory=dict)
    error: ItemError | None = None
    version: int = 0
    created_at: datetime
    updated_at: datetime

    def public(self):
        return {"ordinal": self.ordinal, "name": self.name, "status": self.status,
                **(self.statistics.model_dump() if self.statistics else {}),
                "artifacts": dict(self.artifacts), "error": self.error.model_dump() if self.error else None}


def batch_status(items):
    successful = sum(item["status"] == "SUCCEEDED" for item in items)
    return "SUCCEEDED" if successful == len(items) else "PARTIALLY_SUCCEEDED" if successful else "FAILED"


def validate_batch(data, status=None):
    if set(data) != {"items"} or not isinstance(data["items"], (tuple, list)) or not 1 <= len(data["items"]) <= 10:
        raise ValueError("Invalid batch data.")
    for ordinal, item in enumerate(data["items"]):
        if item["ordinal"] != ordinal or item["status"] not in {"SUCCEEDED", "FAILED", "SKIPPED"}:
            raise ValueError("Invalid terminal batch order/state.")
        base = {"ordinal", "name", "status", "artifacts", "error"}
        if item["status"] == "SUCCEEDED":
            fields = set(PixelStatistics.model_fields)
            if set(item) != base | fields or item["error"] is not None or set(item["artifacts"]) != {"overlay", "mask"}:
                raise ValueError("Invalid completed item.")
            PixelStatistics.model_validate({key: item[key] for key in fields})
        elif set(item) != base or item["artifacts"] or item["error"] is None:
            raise ValueError("Invalid failed item.")
    if status is not None and batch_status(data["items"]) != status:
        raise ValueError("Batch status disagrees with items.")
