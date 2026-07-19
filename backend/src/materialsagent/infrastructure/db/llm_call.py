from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Final

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    select,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Mapped, Session, mapped_column

from materialsagent.domain.models.llm_call import (
    FAILED,
    PENDING,
    RUNNING,
    SUCCEEDED,
    LLMCall,
)
from materialsagent.infrastructure.db.actor import _raise_safe_persistence_error
from materialsagent.infrastructure.db.base import Base


class LLMCallRow(Base):
    __tablename__ = "llm_call"
    __table_args__ = (
        CheckConstraint(
            "length(btrim(llm_call_id)) > 0",
            name="ck_llm_call_llm_call_id_not_blank",
        ),
        CheckConstraint(
            "length(btrim(task_id)) > 0",
            name="ck_llm_call_task_id_not_blank",
        ),
        CheckConstraint(
            "length(btrim(conversation_id)) > 0",
            name="ck_llm_call_conversation_id_not_blank",
        ),
        CheckConstraint(
            "length(btrim(request_id)) > 0",
            name="ck_llm_call_request_id_not_blank",
        ),
        CheckConstraint(
            "length(btrim(provider)) > 0",
            name="ck_llm_call_provider_not_blank",
        ),
        CheckConstraint(
            "length(btrim(model_name)) > 0",
            name="ck_llm_call_model_name_not_blank",
        ),
        CheckConstraint(
            "input_result_id IS NULL OR length(btrim(input_result_id)) > 0",
            name="ck_llm_call_input_result_id_not_blank",
        ),
        CheckConstraint(
            "prompt_template_id IS NULL OR "
            "length(btrim(prompt_template_id)) > 0",
            name="ck_llm_call_prompt_template_id_not_blank",
        ),
        CheckConstraint(
            "prompt_template_version IS NULL OR "
            "length(btrim(prompt_template_version)) > 0",
            name="ck_llm_call_prompt_template_version_not_blank",
        ),
        CheckConstraint(
            "prompt_digest IS NULL OR prompt_digest ~ '^[0-9a-f]{64}$'",
            name="ck_llm_call_prompt_digest_sha256",
        ),
        CheckConstraint(
            "provider_request_id IS NULL OR "
            "length(btrim(provider_request_id)) > 0",
            name="ck_llm_call_provider_request_id_not_blank",
        ),
        CheckConstraint(
            "error_code IS NULL OR length(btrim(error_code)) > 0",
            name="ck_llm_call_error_code_not_blank",
        ),
        CheckConstraint(
            "safe_error_message IS NULL OR "
            "length(btrim(safe_error_message)) > 0",
            name="ck_llm_call_safe_error_message_not_blank",
        ),
        CheckConstraint(
            "purpose IN ('CHAT_ORCHESTRATION', 'TOOL_RESULT_EXPLANATION')",
            name="ck_llm_call_purpose_allowed",
        ),
        CheckConstraint(
            "(purpose = 'CHAT_ORCHESTRATION' AND input_result_id IS NULL) "
            "OR (purpose = 'TOOL_RESULT_EXPLANATION' "
            "AND input_result_id IS NOT NULL)",
            name="ck_llm_call_input_result_purpose",
        ),
        CheckConstraint(
            "status IN ('PENDING', 'RUNNING', 'SUCCEEDED', 'FAILED')",
            name="ck_llm_call_status_allowed",
        ),
        CheckConstraint(
            "duration_ms IS NULL OR duration_ms >= 0",
            name="ck_llm_call_duration_nonnegative",
        ),
        CheckConstraint(
            "started_at IS NULL OR started_at >= created_at",
            name="ck_llm_call_started_not_before_created",
        ),
        CheckConstraint(
            "completed_at IS NULL OR "
            "(started_at IS NOT NULL AND completed_at >= started_at)",
            name="ck_llm_call_completed_not_before_started",
        ),
        CheckConstraint(
            "(status = 'PENDING' AND started_at IS NULL "
            "AND completed_at IS NULL AND duration_ms IS NULL) OR "
            "(status = 'RUNNING' AND started_at IS NOT NULL "
            "AND completed_at IS NULL AND duration_ms IS NULL) OR "
            "(status IN ('SUCCEEDED', 'FAILED') AND started_at IS NOT NULL "
            "AND completed_at IS NOT NULL AND duration_ms IS NOT NULL)",
            name="ck_llm_call_status_time_shape",
        ),
        CheckConstraint(
            "status <> 'SUCCEEDED' OR "
            "(error_code IS NULL AND safe_error_message IS NULL)",
            name="ck_llm_call_succeeded_without_error",
        ),
        CheckConstraint(
            "status <> 'FAILED' OR error_code IS NOT NULL",
            name="ck_llm_call_failed_requires_error",
        ),
        CheckConstraint(
            "jsonb_typeof(generation_parameters) = 'object'",
            name="ck_llm_call_generation_parameters_object",
        ),
        CheckConstraint(
            "structured_output_summary IS NULL OR "
            "jsonb_typeof(structured_output_summary) = 'object'",
            name="ck_llm_call_structured_output_summary_object",
        ),
        CheckConstraint(
            "usage IS NULL OR jsonb_typeof(usage) = 'object'",
            name="ck_llm_call_usage_object",
        ),
        Index(
            "ix_llm_call_task_request_created",
            "task_id",
            "request_id",
            "created_at",
        ),
    )

    llm_call_id: Mapped[str] = mapped_column(Text, primary_key=True)
    task_id: Mapped[str] = mapped_column(
        ForeignKey(
            "task.task_id",
            name="fk_llm_call_task",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )
    conversation_id: Mapped[str] = mapped_column(
        ForeignKey(
            "conversation.conversation_id",
            name="fk_llm_call_conversation",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )
    request_id: Mapped[str] = mapped_column(Text, nullable=False)
    purpose: Mapped[str] = mapped_column(String(64), nullable=False)
    input_result_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    provider: Mapped[str] = mapped_column(String(128), nullable=False)
    model_name: Mapped[str] = mapped_column(String(256), nullable=False)
    prompt_template_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    prompt_template_version: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
    )
    prompt_digest: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
    )
    generation_parameters: Mapped[dict[str, object]] = mapped_column(
        JSONB,
        nullable=False,
    )
    structured_output_summary: Mapped[dict[str, object] | None] = mapped_column(
        JSONB(none_as_null=True),
        nullable=True,
    )
    usage: Mapped[dict[str, object] | None] = mapped_column(
        JSONB(none_as_null=True),
        nullable=True,
    )
    provider_request_id: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error_code: Mapped[str | None] = mapped_column(
        String(128),
        nullable=True,
    )
    safe_error_message: Mapped[str | None] = mapped_column(Text, nullable=True)


def _jsonb_container(value: object) -> object:
    if isinstance(value, Mapping):
        return {key: _jsonb_container(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_jsonb_container(item) for item in value]
    if value is None or type(value) in (str, bool, int, float):
        return value
    raise ValueError("LLM metadata is not JSON-ready.")


def _from_row(row: LLMCallRow) -> LLMCall:
    return LLMCall(
        llm_call_id=row.llm_call_id,
        task_id=row.task_id,
        conversation_id=row.conversation_id,
        request_id=row.request_id,
        purpose=row.purpose,
        input_result_id=row.input_result_id,
        provider=row.provider,
        model_name=row.model_name,
        prompt_template_id=row.prompt_template_id,
        prompt_template_version=row.prompt_template_version,
        prompt_digest=row.prompt_digest,
        generation_parameters=dict(row.generation_parameters),
        structured_output_summary=(
            dict(row.structured_output_summary)
            if row.structured_output_summary is not None
            else None
        ),
        usage=dict(row.usage) if row.usage is not None else None,
        provider_request_id=row.provider_request_id,
        status=row.status,
        created_at=row.created_at,
        started_at=row.started_at,
        completed_at=row.completed_at,
        duration_ms=row.duration_ms,
        error_code=row.error_code,
        safe_error_message=row.safe_error_message,
    )


def _immutable_matches(row: LLMCallRow, call: LLMCall) -> bool:
    scalar_fields_match = all(
        getattr(row, field_name) == getattr(call, field_name)
        for field_name in (
            "llm_call_id",
            "task_id",
            "conversation_id",
            "request_id",
            "purpose",
            "input_result_id",
            "provider",
            "model_name",
            "prompt_template_id",
            "prompt_template_version",
            "prompt_digest",
            "created_at",
        )
    )
    return scalar_fields_match and row.generation_parameters == _jsonb_container(
        call.generation_parameters
    )


ALLOWED_TRANSITIONS: Final = {
    PENDING: frozenset({RUNNING, FAILED}),
    RUNNING: frozenset({SUCCEEDED, FAILED}),
    SUCCEEDED: frozenset(),
    FAILED: frozenset(),
}


class SQLAlchemyLLMCallRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def get(self, llm_call_id: str) -> LLMCall | None:
        try:
            row = self._session.get(LLMCallRow, llm_call_id)
        except SQLAlchemyError as error:
            _raise_safe_persistence_error(error)
        return None if row is None else _from_row(row)

    def add(self, call: LLMCall) -> None:
        try:
            self._session.add(
                LLMCallRow(
                    llm_call_id=call.llm_call_id,
                    task_id=call.task_id,
                    conversation_id=call.conversation_id,
                    request_id=call.request_id,
                    purpose=call.purpose,
                    input_result_id=call.input_result_id,
                    provider=call.provider,
                    model_name=call.model_name,
                    prompt_template_id=call.prompt_template_id,
                    prompt_template_version=call.prompt_template_version,
                    prompt_digest=call.prompt_digest,
                    generation_parameters=_jsonb_container(
                        call.generation_parameters
                    ),
                    structured_output_summary=_jsonb_container(
                        call.structured_output_summary
                    ),
                    usage=_jsonb_container(call.usage),
                    provider_request_id=call.provider_request_id,
                    status=call.status,
                    created_at=call.created_at,
                    started_at=call.started_at,
                    completed_at=call.completed_at,
                    duration_ms=call.duration_ms,
                    error_code=call.error_code,
                    safe_error_message=call.safe_error_message,
                )
            )
        except SQLAlchemyError as error:
            _raise_safe_persistence_error(error)

    def list_for_task(
        self,
        task_id: str,
        *,
        request_id: str | None = None,
    ) -> list[LLMCall]:
        statement = select(LLMCallRow).where(LLMCallRow.task_id == task_id)
        if request_id is not None:
            statement = statement.where(LLMCallRow.request_id == request_id)
        statement = statement.order_by(
            LLMCallRow.created_at.asc(),
            LLMCallRow.llm_call_id.asc(),
        )
        try:
            rows = self._session.scalars(statement).all()
        except SQLAlchemyError as error:
            _raise_safe_persistence_error(error)
        return [_from_row(row) for row in rows]

    def update(
        self,
        call: LLMCall,
        *,
        expected_status: str,
    ) -> LLMCall | None:
        try:
            row = self._session.get(
                LLMCallRow,
                call.llm_call_id,
                populate_existing=True,
                with_for_update=True,
            )
            if row is None or row.status != expected_status:
                return None
            if not _immutable_matches(row, call):
                return None
            if call.status not in ALLOWED_TRANSITIONS.get(row.status, frozenset()):
                return None
            row.structured_output_summary = _jsonb_container(
                call.structured_output_summary
            )
            row.usage = _jsonb_container(call.usage)
            row.provider_request_id = call.provider_request_id
            row.status = call.status
            row.started_at = call.started_at
            row.completed_at = call.completed_at
            row.duration_ms = call.duration_ms
            row.error_code = call.error_code
            row.safe_error_message = call.safe_error_message
            return _from_row(row)
        except SQLAlchemyError as error:
            _raise_safe_persistence_error(error)
