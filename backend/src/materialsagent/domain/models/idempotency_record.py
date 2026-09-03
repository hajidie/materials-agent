from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import re
import unicodedata

_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
CONVERSATION_CREATE = "CONVERSATION_CREATE"
TASK_CREATE = "TASK_CREATE"
TASK_INPUT_SUPPLEMENT = "TASK_INPUT_SUPPLEMENT"
TOOL_RETRY = "TOOL_RETRY"
EXPLANATION_RETRY = "EXPLANATION_RETRY"
IDEMPOTENCY_OPERATIONS = frozenset(
    {
        CONVERSATION_CREATE,
        TASK_CREATE,
        TASK_INPUT_SUPPLEMENT,
        TOOL_RETRY,
        EXPLANATION_RETRY,
    }
)


def _require_id(value: str | None, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be non-blank text.")


def _require_optional_id(value: str | None, field_name: str) -> None:
    if value is not None:
        _require_id(value, field_name)


def _require_utc(value: datetime, field_name: str) -> None:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
        or value.utcoffset() != timezone.utc.utcoffset(value)
    ):
        raise ValueError(f"{field_name} must be timezone-aware UTC.")


def _require_key(value: str) -> None:
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value) > 255
        or any(
            unicodedata.category(character) == "Cc"
            for character in value
        )
    ):
        raise ValueError("idempotency_key is invalid.")


@dataclass(frozen=True, slots=True)
class IdempotencyRecord:
    idempotency_record_id: str
    actor_id: str
    operation: str
    idempotency_key: str
    request_digest: str
    first_request_id: str
    task_id: str | None
    message_id: str | None
    task_input_revision_id: str | None
    tool_run_id: str | None
    explanation_id: str | None
    created_at: datetime
    expires_at: datetime | None
    conversation_id: str | None = None

    def __post_init__(self) -> None:
        for field_name in (
            "idempotency_record_id",
            "actor_id",
            "first_request_id",
        ):
            _require_id(getattr(self, field_name), field_name)
        if self.operation not in IDEMPOTENCY_OPERATIONS:
            raise ValueError("operation is not supported.")
        _require_key(self.idempotency_key)
        if (
            not isinstance(self.request_digest, str)
            or _SHA256.fullmatch(self.request_digest) is None
        ):
            raise ValueError("request_digest must be lowercase SHA-256 hex.")
        for field_name in (
            "conversation_id",
            "task_id",
            "message_id",
            "task_input_revision_id",
            "tool_run_id",
            "explanation_id",
        ):
            _require_optional_id(getattr(self, field_name), field_name)
        _require_utc(self.created_at, "created_at")
        if self.expires_at is not None:
            _require_utc(self.expires_at, "expires_at")
        expected = {
            CONVERSATION_CREATE: (
                self.conversation_id is not None
                and self.task_id is None
                and self.message_id is None
                and self.task_input_revision_id is None
                and self.tool_run_id is None
                and self.explanation_id is None
            ),
            TASK_CREATE: (
                self.task_id is not None
                and self.message_id is not None
                and self.task_input_revision_id is None
                and self.tool_run_id is None
                and self.explanation_id is None
            ),
            TASK_INPUT_SUPPLEMENT: (
                self.task_id is not None
                and self.message_id is not None
                and self.tool_run_id is None
                and self.explanation_id is None
            ),
            TOOL_RETRY: (
                self.conversation_id is None
                and
                self.task_id is not None
                and self.message_id is None
                and self.task_input_revision_id is None
                and self.tool_run_id is not None
                and self.explanation_id is None
            ),
            EXPLANATION_RETRY: (
                self.conversation_id is None
                and
                self.task_id is not None
                and self.message_id is None
                and self.task_input_revision_id is None
                and self.tool_run_id is None
                and self.explanation_id is not None
            ),
        }[self.operation]
        if not expected:
            raise ValueError("resource binding does not match operation.")
