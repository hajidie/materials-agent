from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Final


PENDING: Final = "PENDING"
RUNNING: Final = "RUNNING"
NEEDS_INPUT: Final = "NEEDS_INPUT"
SUCCEEDED: Final = "SUCCEEDED"
PARTIALLY_SUCCEEDED: Final = "PARTIALLY_SUCCEEDED"
FAILED: Final = "FAILED"

KNOWLEDGE_QA: Final = "KNOWLEDGE_QA"
TOOL_EXECUTION: Final = "TOOL_EXECUTION"

TASK_STATUSES: Final = frozenset(
    {
        PENDING,
        RUNNING,
        NEEDS_INPUT,
        SUCCEEDED,
        PARTIALLY_SUCCEEDED,
        FAILED,
    }
)
TASK_TYPES: Final = frozenset({KNOWLEDGE_QA, TOOL_EXECUTION})


def _require_non_blank(value: str, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty opaque text ID.")


def _require_utc(value: datetime, field_name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware UTC.")
    if value.utcoffset() != timezone.utc.utcoffset(value):
        raise ValueError(f"{field_name} must be timezone-aware UTC.")


@dataclass(slots=True)
class Task:
    task_id: str
    conversation_id: str
    actor_id: str
    task_type: str | None
    current_status: str
    selected_tool_run_id: str | None
    selected_result_id: str | None
    created_at: datetime
    started_at: datetime | None
    updated_at: datetime
    completed_at: datetime | None
    error_code: str | None
    safe_error_message: str | None

    def __post_init__(self) -> None:
        _require_non_blank(self.task_id, "task_id")
        _require_non_blank(self.conversation_id, "conversation_id")
        _require_non_blank(self.actor_id, "actor_id")
        if self.task_type is not None and self.task_type not in TASK_TYPES:
            raise ValueError(
                "task_type must be null, KNOWLEDGE_QA, or TOOL_EXECUTION."
            )
        if self.current_status not in TASK_STATUSES:
            raise ValueError("current_status is not an allowed Task status.")
        if self.selected_tool_run_id is not None:
            _require_non_blank(
                self.selected_tool_run_id,
                "selected_tool_run_id",
            )
        if self.selected_result_id is not None:
            _require_non_blank(self.selected_result_id, "selected_result_id")
            if self.selected_tool_run_id is None:
                raise ValueError(
                    "selected_tool_run_id is required when selected_result_id is set."
                )

        _require_utc(self.created_at, "created_at")
        _require_utc(self.updated_at, "updated_at")
        if self.started_at is not None:
            _require_utc(self.started_at, "started_at")
        if self.completed_at is not None:
            _require_utc(self.completed_at, "completed_at")
        if self.updated_at < self.created_at:
            raise ValueError("updated_at must not be earlier than created_at.")
        if self.started_at is not None and self.started_at < self.created_at:
            raise ValueError("started_at must not be earlier than created_at.")
        if self.completed_at is not None and self.completed_at < self.created_at:
            raise ValueError("completed_at must not be earlier than created_at.")
        if (
            self.completed_at is not None
            and self.started_at is not None
            and self.completed_at < self.started_at
        ):
            raise ValueError("completed_at must not be earlier than started_at.")

    @classmethod
    def pending(
        cls,
        *,
        task_id: str,
        conversation_id: str,
        actor_id: str,
        created_at: datetime | None = None,
    ) -> Task:
        timestamp = created_at or datetime.now(timezone.utc)
        return cls(
            task_id=task_id,
            conversation_id=conversation_id,
            actor_id=actor_id,
            task_type=None,
            current_status=PENDING,
            selected_tool_run_id=None,
            selected_result_id=None,
            created_at=timestamp,
            started_at=None,
            updated_at=timestamp,
            completed_at=None,
            error_code=None,
            safe_error_message=None,
        )
