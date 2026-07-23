from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Final


PENDING: Final = "PENDING"
RUNNING: Final = "RUNNING"
SUCCEEDED: Final = "SUCCEEDED"
FAILED: Final = "FAILED"
EXPLANATION_STATUSES: Final = frozenset({PENDING, RUNNING, SUCCEEDED, FAILED})
MAX_SAFE_TEXT_CHARS: Final = 4096
MAX_SAFE_ERROR_CHARS: Final = 256


def _require_text(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be non-blank text.")
    return value


def _require_safe_text(value: object, field_name: str, maximum: int) -> str:
    text = _require_text(value, field_name)
    if len(text) > maximum or not text.isprintable():
        raise ValueError(f"{field_name} must be bounded safe text.")
    return text


def _require_utc(value: object, field_name: str) -> datetime:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
        or value.utcoffset() != timezone.utc.utcoffset(value)
    ):
        raise ValueError(f"{field_name} must be timezone-aware UTC.")
    return value


@dataclass(frozen=True, slots=True)
class NaturalLanguageExplanation:
    explanation_id: str
    task_id: str
    result_id: str
    llm_call_id: str
    attempt_no: int
    status: str
    language: str
    text: str | None
    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None
    duration_ms: int | None
    error_code: str | None
    safe_error_message: str | None

    def __post_init__(self) -> None:
        for field_name in ("explanation_id", "task_id", "result_id", "llm_call_id"):
            _require_text(getattr(self, field_name), field_name)
        _require_safe_text(self.language, "language", 32)
        if (
            not isinstance(self.attempt_no, int)
            or isinstance(self.attempt_no, bool)
            or self.attempt_no <= 0
        ):
            raise ValueError("attempt_no must be a positive integer.")
        if self.status not in EXPLANATION_STATUSES:
            raise ValueError("status is not an allowed Explanation status.")
        created_at = _require_utc(self.created_at, "created_at")
        if self.started_at is not None:
            started_at = _require_utc(self.started_at, "started_at")
            if started_at < created_at:
                raise ValueError("started_at must not be earlier than created_at.")
        if self.completed_at is not None:
            completed_at = _require_utc(self.completed_at, "completed_at")
            if self.started_at is None or completed_at < self.started_at:
                raise ValueError("completed_at must not be earlier than started_at.")
        if self.duration_ms is not None and (
            not isinstance(self.duration_ms, int)
            or isinstance(self.duration_ms, bool)
            or self.duration_ms < 0
        ):
            raise ValueError("duration_ms must be a nonnegative integer or null.")
        if self.text is not None:
            _require_safe_text(self.text, "text", MAX_SAFE_TEXT_CHARS)
        if self.error_code is not None:
            _require_safe_text(self.error_code, "error_code", 64)
        if self.safe_error_message is not None:
            _require_safe_text(
                self.safe_error_message,
                "safe_error_message",
                MAX_SAFE_ERROR_CHARS,
            )
        if self.status == PENDING and any(
            value is not None
            for value in (
                self.started_at,
                self.completed_at,
                self.duration_ms,
                self.text,
                self.error_code,
                self.safe_error_message,
            )
        ):
            raise ValueError("PENDING cannot contain execution outcome fields.")
        if self.status == RUNNING:
            if self.started_at is None:
                raise ValueError("RUNNING requires started_at.")
            if any(
                value is not None
                for value in (
                    self.completed_at,
                    self.duration_ms,
                    self.text,
                    self.error_code,
                    self.safe_error_message,
                )
            ):
                raise ValueError("RUNNING cannot contain terminal outcome fields.")
        if self.status == SUCCEEDED and (
            self.started_at is None
            or self.completed_at is None
            or self.duration_ms is None
            or self.text is None
            or self.error_code is not None
            or self.safe_error_message is not None
        ):
            raise ValueError("SUCCEEDED requires completed timing and text only.")
        if self.status == FAILED and (
            self.started_at is None
            or self.completed_at is None
            or self.duration_ms is None
            or self.text is not None
            or self.error_code is None
            or self.safe_error_message is None
        ):
            raise ValueError("FAILED requires completed timing and a safe error only.")
