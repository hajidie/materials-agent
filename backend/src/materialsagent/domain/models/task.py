from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from typing import Final

from materialsagent.domain.ports.tool_registry import ToolRef


class TaskStatus(StrEnum):
    PENDING = "PENDING"
    NEEDS_INPUT = "NEEDS_INPUT"
    READY = "READY"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    PARTIALLY_SUCCEEDED = "PARTIALLY_SUCCEEDED"
    FAILED = "FAILED"


PENDING: Final = TaskStatus.PENDING
NEEDS_INPUT: Final = TaskStatus.NEEDS_INPUT
READY: Final = TaskStatus.READY
RUNNING: Final = TaskStatus.RUNNING
SUCCEEDED: Final = TaskStatus.SUCCEEDED
PARTIALLY_SUCCEEDED: Final = TaskStatus.PARTIALLY_SUCCEEDED
FAILED: Final = TaskStatus.FAILED

KNOWLEDGE_QA: Final = "KNOWLEDGE_QA"
TOOL_EXECUTION: Final = "TOOL_EXECUTION"

TASK_STATUSES: Final = frozenset(
    {
        PENDING,
        RUNNING,
        NEEDS_INPUT,
        READY,
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
    tool_id: str | None = None
    bound_tool_version: str | None = None
    bound_schema_hash: str | None = None

    @property
    def bound_tool_ref(self) -> ToolRef | None:
        if self.tool_id is None:
            return None
        return ToolRef(
            tool_id=self.tool_id,
            version=self.bound_tool_version or "",
            schema_hash=self.bound_schema_hash or "",
        )

    def __post_init__(self) -> None:
        _require_non_blank(self.task_id, "task_id")
        _require_non_blank(self.conversation_id, "conversation_id")
        _require_non_blank(self.actor_id, "actor_id")
        if self.task_type is not None and self.task_type not in TASK_TYPES:
            raise ValueError(
                "task_type must be null, KNOWLEDGE_QA, or TOOL_EXECUTION."
            )
        try:
            status = TaskStatus(self.current_status)
        except ValueError:
            raise ValueError("current_status is not an allowed Task status.")
        self.current_status = status
        binding = (
            self.tool_id,
            self.bound_tool_version,
            self.bound_schema_hash,
        )
        if any(value is None for value in binding) and any(
            value is not None for value in binding
        ):
            raise ValueError("Tool binding must be all present or all absent.")
        if self.tool_id is not None:
            _require_non_blank(self.tool_id, "tool_id")
            _require_non_blank(self.bound_tool_version or "", "bound_tool_version")
            if (
                self.bound_schema_hash is None
                or len(self.bound_schema_hash) != 64
                or any(character not in "0123456789abcdef" for character in self.bound_schema_hash)
            ):
                raise ValueError("bound_schema_hash must be lowercase SHA-256 hex.")
        if self.current_status == READY and self.bound_tool_ref is None:
            raise ValueError("READY requires a bound Tool.")
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

    def bind_tool(self, ref: ToolRef) -> None:
        proposed = (ref.tool_id, ref.version, ref.schema_hash)
        for value, field_name in zip(
            proposed,
            ("tool_id", "bound_tool_version", "bound_schema_hash"),
            strict=True,
        ):
            _require_non_blank(value, field_name)
        if len(ref.schema_hash) != 64 or any(
            character not in "0123456789abcdef"
            for character in ref.schema_hash
        ):
            raise ValueError("bound_schema_hash must be lowercase SHA-256 hex.")
        current = (
            self.tool_id,
            self.bound_tool_version,
            self.bound_schema_hash,
        )
        if current != (None, None, None) and current != proposed:
            raise TaskToolBindingConflictError(self.task_id)
        (
            self.tool_id,
            self.bound_tool_version,
            self.bound_schema_hash,
        ) = proposed

    def validate_routing_state(self, latest_revision: object) -> None:
        from materialsagent.domain.models.task_input_revision import (
            TaskInputRevision,
        )

        if not isinstance(latest_revision, TaskInputRevision):
            raise TaskRoutingStateError(self.task_id)
        if latest_revision.task_id != self.task_id:
            raise TaskRoutingStateError(self.task_id)
        if self.current_status == READY:
            if self.bound_tool_ref is None:
                raise TaskRoutingStateError(self.task_id, "READY requires a bound Tool.")
            if not latest_revision.is_complete:
                raise TaskRoutingStateError(
                    self.task_id,
                    "READY requires a complete latest input revision.",
                )
        if self.current_status == NEEDS_INPUT:
            if self.bound_tool_ref is None and len(latest_revision.candidate_tool_refs) < 2:
                raise TaskRoutingStateError(
                    self.task_id,
                    "NEEDS_INPUT requires candidate Tool references.",
                )
            if (
                self.bound_tool_ref is not None
                and not latest_revision.missing_fields
                and not latest_revision.ambiguous_fields
                and not latest_revision.validation_errors
            ):
                raise TaskRoutingStateError(
                    self.task_id,
                    "Bound NEEDS_INPUT requires an unresolved input field; "
                    "a complete bound revision must be READY.",
                )


class TaskToolBindingConflictError(RuntimeError):
    def __init__(self, task_id: str) -> None:
        super().__init__(f"Task Tool binding conflict: {task_id}")


class TaskRoutingStateError(ValueError):
    def __init__(self, task_id: str, message: str = "Task routing state is invalid.") -> None:
        self.task_id = task_id
        super().__init__(message)
