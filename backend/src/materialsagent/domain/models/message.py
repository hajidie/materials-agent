from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Final


USER: Final = "USER"
ASSISTANT: Final = "ASSISTANT"
LLM: Final = "LLM"
TEMPLATE: Final = "TEMPLATE"

MESSAGE_ROLES: Final = frozenset({USER, ASSISTANT})
GENERATION_SOURCES: Final = frozenset({USER, LLM, TEMPLATE})


def _require_non_blank(value: str, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be non-blank text.")


def _require_utc(value: datetime, field_name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware UTC.")
    if value.utcoffset() != timezone.utc.utcoffset(value):
        raise ValueError(f"{field_name} must be timezone-aware UTC.")


@dataclass(frozen=True, slots=True)
class Message:
    message_id: str
    conversation_id: str
    task_id: str | None
    actor_id: str
    request_id: str
    role: str
    generation_source: str
    content_text: str
    structured_content: dict[str, object] | None
    llm_call_id: str | None
    created_at: datetime

    def __post_init__(self) -> None:
        for field_name in (
            "message_id",
            "conversation_id",
            "actor_id",
            "request_id",
        ):
            _require_non_blank(getattr(self, field_name), field_name)
        if self.task_id is not None:
            _require_non_blank(self.task_id, "task_id")
        if self.role not in MESSAGE_ROLES:
            raise ValueError("role must be USER or ASSISTANT.")
        if self.generation_source not in GENERATION_SOURCES:
            raise ValueError(
                "generation_source must be USER, LLM, or TEMPLATE."
            )
        _require_non_blank(self.content_text, "content_text")
        if self.structured_content is not None and not isinstance(
            self.structured_content,
            dict,
        ):
            raise ValueError("structured_content must be null or a JSON object.")
        if self.llm_call_id is not None:
            _require_non_blank(self.llm_call_id, "llm_call_id")

        if self.role == USER:
            if self.generation_source != USER or self.llm_call_id is not None:
                raise ValueError(
                    "USER messages require USER generation and no llm_call_id."
                )
        if self.generation_source == USER and self.role != USER:
            raise ValueError("USER generation is only valid for USER messages.")
        if self.generation_source == LLM:
            if self.role != ASSISTANT or self.llm_call_id is None:
                raise ValueError(
                    "LLM generation requires an ASSISTANT role and llm_call_id."
                )
        if self.generation_source == TEMPLATE and self.role != ASSISTANT:
            raise ValueError(
                "TEMPLATE generation is only valid for ASSISTANT messages."
            )
        _require_utc(self.created_at, "created_at")

    @classmethod
    def user(
        cls,
        *,
        message_id: str,
        conversation_id: str,
        task_id: str | None,
        actor_id: str,
        request_id: str,
        content_text: str,
        created_at: datetime | None = None,
        structured_content: dict[str, object] | None = None,
    ) -> Message:
        return cls(
            message_id=message_id,
            conversation_id=conversation_id,
            task_id=task_id,
            actor_id=actor_id,
            request_id=request_id,
            role=USER,
            generation_source=USER,
            content_text=content_text,
            structured_content=structured_content,
            llm_call_id=None,
            created_at=created_at or datetime.now(timezone.utc),
        )
