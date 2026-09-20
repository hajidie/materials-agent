from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone


def _require_non_blank(value: str, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty opaque text ID.")


def _require_utc(value: datetime, field_name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware UTC.")
    if value.utcoffset() != timezone.utc.utcoffset(value):
        raise ValueError(f"{field_name} must be timezone-aware UTC.")


@dataclass(slots=True)
class Conversation:
    conversation_id: str
    actor_id: str
    title: str | None
    created_at: datetime
    updated_at: datetime
    deletion_fence_operation_id: str | None = None
    deletion_fence_version: int = 0

    def __post_init__(self) -> None:
        _require_non_blank(self.conversation_id, "conversation_id")
        _require_non_blank(self.actor_id, "actor_id")
        if self.title is not None:
            if not isinstance(self.title, str) or not self.title.strip():
                raise ValueError("title must be null or non-blank text.")
        _require_utc(self.created_at, "created_at")
        _require_utc(self.updated_at, "updated_at")
        if self.updated_at < self.created_at:
            raise ValueError("updated_at must not be earlier than created_at.")
