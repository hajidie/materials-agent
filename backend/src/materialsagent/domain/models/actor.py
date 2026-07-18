from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Final


LOCAL_ANONYMOUS: Final = "LOCAL_ANONYMOUS"


def _require_utc(value: datetime, field_name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware UTC.")
    if value.utcoffset() != timezone.utc.utcoffset(value):
        raise ValueError(f"{field_name} must be timezone-aware UTC.")


@dataclass(frozen=True, slots=True)
class Actor:
    actor_id: str
    user_id: str | None
    actor_origin: str
    created_at: datetime
    linked_at: datetime | None

    def __post_init__(self) -> None:
        if not isinstance(self.actor_id, str) or not self.actor_id.strip():
            raise ValueError("actor_id must be a non-empty opaque text ID.")
        if self.actor_origin != LOCAL_ANONYMOUS:
            raise ValueError("actor_origin must be LOCAL_ANONYMOUS.")
        _require_utc(self.created_at, "created_at")
        if self.linked_at is not None:
            _require_utc(self.linked_at, "linked_at")

    @classmethod
    def local_anonymous(
        cls,
        actor_id: str,
        *,
        created_at: datetime | None = None,
    ) -> Actor:
        return cls(
            actor_id=actor_id,
            user_id=None,
            actor_origin=LOCAL_ANONYMOUS,
            created_at=created_at or datetime.now(timezone.utc),
            linked_at=None,
        )
