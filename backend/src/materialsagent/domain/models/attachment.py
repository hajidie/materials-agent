"""Transport handles for message attachments, never model arguments."""
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field


class Attachment(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    attachment_id: str = Field(min_length=1, max_length=128)
    kind: Literal["dataset", "ebsd_image"]
    name: str = Field(min_length=1, max_length=200)
