from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Final

from materialsagent.domain.models.asset import STORAGE_IDENTITY_VERSIONS
from materialsagent.domain.ports.storage import validate_object_key


PENDING: Final = "PENDING"
COMPLETED: Final = "COMPLETED"
SAFETY_BLOCKED: Final = "SAFETY_BLOCKED"
CLEANUP_STATUSES: Final = frozenset({PENDING, COMPLETED, SAFETY_BLOCKED})


def _require_text(value: str, name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be non-blank text.")


def _require_utc(value: datetime | None, name: str) -> None:
    if value is None:
        return
    if (
        value.tzinfo is None
        or value.utcoffset() is None
        or value.utcoffset() != timezone.utc.utcoffset(value)
    ):
        raise ValueError(f"{name} must be timezone-aware UTC.")


@dataclass(frozen=True, slots=True)
class ConversationObjectCleanup:
    cleanup_id: str
    actor_id: str
    conversation_id: str
    asset_id: str
    operation_id: str
    producer_tool_run_id: str | None
    object_key: str
    bucket: str
    storage_namespace: str
    identity_version: str
    status: str
    attempts: int
    created_at: datetime
    last_attempt_at: datetime | None
    completed_at: datetime | None
    safety_error_code: str | None

    def __post_init__(self) -> None:
        for field_name in (
            "cleanup_id",
            "actor_id",
            "conversation_id",
            "asset_id",
            "operation_id",
            "object_key",
            "bucket",
            "storage_namespace",
        ):
            _require_text(getattr(self, field_name), field_name)
        if self.producer_tool_run_id is not None:
            _require_text(self.producer_tool_run_id, "producer_tool_run_id")
        validate_object_key(self.object_key)
        if self.identity_version not in STORAGE_IDENTITY_VERSIONS:
            raise ValueError("identity_version is not supported.")
        if self.status not in CLEANUP_STATUSES:
            raise ValueError("cleanup status is not supported.")
        if not isinstance(self.attempts, int) or self.attempts < 0:
            raise ValueError("attempts must be a nonnegative integer.")
        _require_utc(self.created_at, "created_at")
        _require_utc(self.last_attempt_at, "last_attempt_at")
        _require_utc(self.completed_at, "completed_at")
        if self.safety_error_code is not None:
            _require_text(self.safety_error_code, "safety_error_code")
        if self.status == COMPLETED and self.completed_at is None:
            raise ValueError("completed cleanup requires completed_at.")
        if self.status == SAFETY_BLOCKED and self.safety_error_code is None:
            raise ValueError("blocked cleanup requires a safety error code.")

    def pending_after(self, *, attempted_at: datetime, error_code: str) -> "ConversationObjectCleanup":
        return replace(
            self,
            status=PENDING,
            attempts=self.attempts + 1,
            last_attempt_at=attempted_at,
            safety_error_code=error_code,
        )

    def complete(self, *, completed_at: datetime) -> "ConversationObjectCleanup":
        return replace(
            self,
            status=COMPLETED,
            attempts=self.attempts + 1,
            last_attempt_at=completed_at,
            completed_at=completed_at,
            safety_error_code=None,
        )

    def block(self, *, attempted_at: datetime, error_code: str) -> "ConversationObjectCleanup":
        return replace(
            self,
            status=SAFETY_BLOCKED,
            attempts=self.attempts + 1,
            last_attempt_at=attempted_at,
            safety_error_code=error_code,
        )
