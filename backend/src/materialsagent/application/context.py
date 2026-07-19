from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ActorContext:
    actor_id: str
    user_id: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.actor_id, str) or not self.actor_id.strip():
            raise ValueError("actor_id must be non-blank opaque text.")
        if self.user_id is not None:
            raise ValueError("user_id must be null in the local MVP.")
