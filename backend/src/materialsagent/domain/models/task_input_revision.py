from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone


def _require_non_blank(value: str, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be non-blank text.")


def _require_utc(value: datetime, field_name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware UTC.")
    if value.utcoffset() != timezone.utc.utcoffset(value):
        raise ValueError(f"{field_name} must be timezone-aware UTC.")


@dataclass(frozen=True, slots=True)
class TaskInputRevision:
    task_input_revision_id: str
    task_id: str
    request_id: str
    source_llm_call_id: str | None
    source_message_ids: list[str]
    revision: int
    raw_input: dict[str, object]
    normalized_input: dict[str, object] | None
    missing_fields: list[str]
    ambiguous_fields: list[dict[str, object]]
    validation_errors: list[dict[str, object]]
    created_at: datetime

    def __post_init__(self) -> None:
        _require_non_blank(
            self.task_input_revision_id,
            "task_input_revision_id",
        )
        _require_non_blank(self.task_id, "task_id")
        _require_non_blank(self.request_id, "request_id")
        if self.source_llm_call_id is not None:
            _require_non_blank(self.source_llm_call_id, "source_llm_call_id")
        if not isinstance(self.source_message_ids, list) or not self.source_message_ids:
            raise ValueError("source_message_ids must contain at least one ID.")
        for source_message_id in self.source_message_ids:
            _require_non_blank(source_message_id, "source_message_ids item")
        if (
            not isinstance(self.revision, int)
            or isinstance(self.revision, bool)
            or self.revision <= 0
        ):
            raise ValueError("revision must be a positive integer.")
        if not isinstance(self.raw_input, dict):
            raise ValueError("raw_input must be a JSON object.")
        if self.normalized_input is not None and not isinstance(
            self.normalized_input,
            dict,
        ):
            raise ValueError("normalized_input must be null or a JSON object.")
        if not isinstance(self.missing_fields, list) or not all(
            isinstance(field, str) for field in self.missing_fields
        ):
            raise ValueError("missing_fields must be a string array.")
        if not isinstance(self.ambiguous_fields, list) or not all(
            isinstance(field, dict) for field in self.ambiguous_fields
        ):
            raise ValueError("ambiguous_fields must be a structured array.")
        if not isinstance(self.validation_errors, list) or not all(
            isinstance(error, dict) for error in self.validation_errors
        ):
            raise ValueError("validation_errors must be a structured array.")
        _require_utc(self.created_at, "created_at")
