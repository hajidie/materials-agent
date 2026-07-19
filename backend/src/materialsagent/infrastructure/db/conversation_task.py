from __future__ import annotations

from datetime import datetime
from typing import Final

from sqlalchemy import (
    ARRAY,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    select,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Mapped, Session, mapped_column

from materialsagent.domain.models.conversation import Conversation
from materialsagent.domain.models.message import Message
from materialsagent.domain.models.task import Task
from materialsagent.domain.models.task_input_revision import TaskInputRevision
from materialsagent.infrastructure.db.actor import _raise_safe_persistence_error
from materialsagent.infrastructure.db.base import Base


class ConversationRow(Base):
    __tablename__ = "conversation"
    __table_args__ = (
        CheckConstraint(
            "length(btrim(conversation_id)) > 0",
            name="ck_conversation_conversation_id_not_blank",
        ),
        CheckConstraint(
            "length(btrim(actor_id)) > 0",
            name="ck_conversation_actor_id_not_blank",
        ),
        CheckConstraint(
            "title IS NULL OR length(btrim(title)) > 0",
            name="ck_conversation_title_not_blank",
        ),
        CheckConstraint(
            "updated_at >= created_at",
            name="ck_conversation_updated_at_not_before_created_at",
        ),
    )

    conversation_id: Mapped[str] = mapped_column(Text, primary_key=True)
    actor_id: Mapped[str] = mapped_column(
        ForeignKey(
            "actor.actor_id",
            name="fk_conversation_actor",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )
    title: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )


class TaskRow(Base):
    __tablename__ = "task"
    __table_args__ = (
        CheckConstraint(
            "length(btrim(task_id)) > 0",
            name="ck_task_task_id_not_blank",
        ),
        CheckConstraint(
            "length(btrim(conversation_id)) > 0",
            name="ck_task_conversation_id_not_blank",
        ),
        CheckConstraint(
            "length(btrim(actor_id)) > 0",
            name="ck_task_actor_id_not_blank",
        ),
        CheckConstraint(
            "task_type IS NULL OR task_type IN "
            "('KNOWLEDGE_QA', 'TOOL_EXECUTION')",
            name="ck_task_task_type_allowed",
        ),
        CheckConstraint(
            "current_status IN "
            "('PENDING', 'RUNNING', 'NEEDS_INPUT', 'SUCCEEDED', "
            "'PARTIALLY_SUCCEEDED', 'FAILED')",
            name="ck_task_current_status_allowed",
        ),
        CheckConstraint(
            "selected_tool_run_id IS NULL OR "
            "length(btrim(selected_tool_run_id)) > 0",
            name="ck_task_selected_tool_run_id_not_blank",
        ),
        CheckConstraint(
            "selected_result_id IS NULL OR "
            "length(btrim(selected_result_id)) > 0",
            name="ck_task_selected_result_id_not_blank",
        ),
        CheckConstraint(
            "selected_result_id IS NULL OR selected_tool_run_id IS NOT NULL",
            name="ck_task_selected_result_requires_tool_run",
        ),
        CheckConstraint(
            "updated_at >= created_at",
            name="ck_task_updated_at_not_before_created_at",
        ),
        CheckConstraint(
            "started_at IS NULL OR started_at >= created_at",
            name="ck_task_started_at_not_before_created_at",
        ),
        CheckConstraint(
            "completed_at IS NULL OR completed_at >= created_at",
            name="ck_task_completed_at_not_before_created_at",
        ),
        CheckConstraint(
            "completed_at IS NULL "
            "OR started_at IS NULL "
            "OR completed_at >= started_at",
            name="ck_task_completed_at_not_before_started_at",
        ),
    )

    task_id: Mapped[str] = mapped_column(Text, primary_key=True)
    conversation_id: Mapped[str] = mapped_column(
        ForeignKey(
            "conversation.conversation_id",
            name="fk_task_conversation",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )
    actor_id: Mapped[str] = mapped_column(
        ForeignKey(
            "actor.actor_id",
            name="fk_task_actor",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )
    task_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    current_status: Mapped[str] = mapped_column(String(64), nullable=False)
    selected_tool_run_id: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
    )
    selected_result_id: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    error_code: Mapped[str | None] = mapped_column(String(128), nullable=True)
    safe_error_message: Mapped[str | None] = mapped_column(Text, nullable=True)


class MessageRow(Base):
    __tablename__ = "message"
    __table_args__ = (
        CheckConstraint(
            "length(btrim(message_id)) > 0",
            name="ck_message_message_id_not_blank",
        ),
        CheckConstraint(
            "length(btrim(conversation_id)) > 0",
            name="ck_message_conversation_id_not_blank",
        ),
        CheckConstraint(
            "length(btrim(task_id)) > 0",
            name="ck_message_task_id_not_blank",
        ),
        CheckConstraint(
            "length(btrim(actor_id)) > 0",
            name="ck_message_actor_id_not_blank",
        ),
        CheckConstraint(
            "length(btrim(request_id)) > 0",
            name="ck_message_request_id_not_blank",
        ),
        CheckConstraint(
            "role IN ('USER', 'ASSISTANT')",
            name="ck_message_role_allowed",
        ),
        CheckConstraint(
            "generation_source IN ('USER', 'LLM', 'TEMPLATE')",
            name="ck_message_generation_source_allowed",
        ),
        CheckConstraint(
            "length(btrim(content_text)) > 0",
            name="ck_message_content_text_not_blank",
        ),
        CheckConstraint(
            "llm_call_id IS NULL OR length(btrim(llm_call_id)) > 0",
            name="ck_message_llm_call_id_not_blank",
        ),
        CheckConstraint(
            "role <> 'USER' OR "
            "(generation_source = 'USER' AND llm_call_id IS NULL)",
            name="ck_message_user_role_source",
        ),
        CheckConstraint(
            "generation_source <> 'USER' OR role = 'USER'",
            name="ck_message_user_source_role",
        ),
        CheckConstraint(
            "generation_source <> 'LLM' OR "
            "(role = 'ASSISTANT' AND llm_call_id IS NOT NULL)",
            name="ck_message_llm_source_role",
        ),
        CheckConstraint(
            "generation_source <> 'TEMPLATE' OR role = 'ASSISTANT'",
            name="ck_message_template_source_role",
        ),
        UniqueConstraint("llm_call_id", name="uq_message_llm_call_id"),
    )

    message_id: Mapped[str] = mapped_column(Text, primary_key=True)
    conversation_id: Mapped[str] = mapped_column(
        ForeignKey(
            "conversation.conversation_id",
            name="fk_message_conversation",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )
    task_id: Mapped[str] = mapped_column(
        ForeignKey(
            "task.task_id",
            name="fk_message_task",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )
    actor_id: Mapped[str] = mapped_column(
        ForeignKey(
            "actor.actor_id",
            name="fk_message_actor",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )
    request_id: Mapped[str] = mapped_column(Text, nullable=False)
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    generation_source: Mapped[str] = mapped_column(String(16), nullable=False)
    content_text: Mapped[str] = mapped_column(Text, nullable=False)
    structured_content: Mapped[dict[str, object] | None] = mapped_column(
        JSONB(none_as_null=True),
        nullable=True,
    )
    llm_call_id: Mapped[str | None] = mapped_column(
        ForeignKey(
            "llm_call.llm_call_id",
            name="fk_message_llm_call",
            ondelete="RESTRICT",
        ),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )


class TaskInputRevisionRow(Base):
    __tablename__ = "task_input_revision"
    __table_args__ = (
        CheckConstraint(
            "length(btrim(task_input_revision_id)) > 0",
            name="ck_task_input_revision_revision_id_not_blank",
        ),
        CheckConstraint(
            "length(btrim(task_id)) > 0",
            name="ck_task_input_revision_task_id_not_blank",
        ),
        CheckConstraint(
            "length(btrim(request_id)) > 0",
            name="ck_task_input_revision_request_id_not_blank",
        ),
        CheckConstraint(
            "source_llm_call_id IS NULL OR "
            "length(btrim(source_llm_call_id)) > 0",
            name="ck_task_input_revision_source_llm_call_id_not_blank",
        ),
        CheckConstraint(
            "revision > 0",
            name="ck_task_input_revision_revision_positive",
        ),
        CheckConstraint(
            "cardinality(source_message_ids) > 0",
            name="ck_task_input_revision_source_message_ids_nonempty",
        ),
        CheckConstraint(
            "jsonb_typeof(raw_input) = 'object'",
            name="ck_task_input_revision_raw_input_object",
        ),
        CheckConstraint(
            "normalized_input IS NULL OR "
            "jsonb_typeof(normalized_input) = 'object'",
            name="ck_task_input_revision_normalized_input_object",
        ),
        CheckConstraint(
            "jsonb_typeof(ambiguous_fields) = 'array'",
            name="ck_task_input_revision_ambiguous_fields_array",
        ),
        CheckConstraint(
            "jsonb_typeof(validation_errors) = 'array'",
            name="ck_task_input_revision_validation_errors_array",
        ),
        UniqueConstraint(
            "task_id",
            "revision",
            name="uq_task_input_revision_task_revision",
        ),
    )

    task_input_revision_id: Mapped[str] = mapped_column(Text, primary_key=True)
    task_id: Mapped[str] = mapped_column(
        ForeignKey(
            "task.task_id",
            name="fk_task_input_revision_task",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )
    request_id: Mapped[str] = mapped_column(Text, nullable=False)
    source_llm_call_id: Mapped[str | None] = mapped_column(
        ForeignKey(
            "llm_call.llm_call_id",
            name="fk_task_input_revision_llm_call",
            ondelete="RESTRICT",
        ),
        nullable=True,
    )
    source_message_ids: Mapped[list[str]] = mapped_column(
        ARRAY(Text),
        nullable=False,
    )
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    raw_input: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    normalized_input: Mapped[dict[str, object] | None] = mapped_column(
        JSONB(none_as_null=True),
        nullable=True,
    )
    missing_fields: Mapped[list[str]] = mapped_column(
        ARRAY(Text),
        nullable=False,
    )
    ambiguous_fields: Mapped[list[dict[str, object]]] = mapped_column(
        JSONB,
        nullable=False,
    )
    validation_errors: Mapped[list[dict[str, object]]] = mapped_column(
        JSONB,
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )


def _conversation_from_row(row: ConversationRow) -> Conversation:
    return Conversation(
        conversation_id=row.conversation_id,
        actor_id=row.actor_id,
        title=row.title,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _message_from_row(row: MessageRow) -> Message:
    return Message(
        message_id=row.message_id,
        conversation_id=row.conversation_id,
        task_id=row.task_id,
        actor_id=row.actor_id,
        request_id=row.request_id,
        role=row.role,
        generation_source=row.generation_source,
        content_text=row.content_text,
        structured_content=row.structured_content,
        llm_call_id=row.llm_call_id,
        created_at=row.created_at,
    )


def _task_from_row(row: TaskRow) -> Task:
    return Task(
        task_id=row.task_id,
        conversation_id=row.conversation_id,
        actor_id=row.actor_id,
        task_type=row.task_type,
        current_status=row.current_status,
        selected_tool_run_id=row.selected_tool_run_id,
        selected_result_id=row.selected_result_id,
        created_at=row.created_at,
        started_at=row.started_at,
        updated_at=row.updated_at,
        completed_at=row.completed_at,
        error_code=row.error_code,
        safe_error_message=row.safe_error_message,
    )


def _revision_from_row(row: TaskInputRevisionRow) -> TaskInputRevision:
    return TaskInputRevision(
        task_input_revision_id=row.task_input_revision_id,
        task_id=row.task_id,
        request_id=row.request_id,
        source_llm_call_id=row.source_llm_call_id,
        source_message_ids=list(row.source_message_ids),
        revision=row.revision,
        raw_input=dict(row.raw_input),
        normalized_input=(
            dict(row.normalized_input)
            if row.normalized_input is not None
            else None
        ),
        missing_fields=list(row.missing_fields),
        ambiguous_fields=list(row.ambiguous_fields),
        validation_errors=list(row.validation_errors),
        created_at=row.created_at,
    )


class SQLAlchemyConversationRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def get(self, conversation_id: str) -> Conversation | None:
        try:
            row = self._session.get(ConversationRow, conversation_id)
        except SQLAlchemyError as error:
            _raise_safe_persistence_error(error)
        return None if row is None else _conversation_from_row(row)

    def get_owned(
        self,
        conversation_id: str,
        actor_id: str,
    ) -> Conversation | None:
        statement = select(ConversationRow).where(
            ConversationRow.conversation_id == conversation_id,
            ConversationRow.actor_id == actor_id,
        )
        try:
            row = self._session.scalar(statement)
        except SQLAlchemyError as error:
            _raise_safe_persistence_error(error)
        return None if row is None else _conversation_from_row(row)

    def list_owned(self, actor_id: str) -> list[Conversation]:
        statement = (
            select(ConversationRow)
            .where(ConversationRow.actor_id == actor_id)
            .order_by(
                ConversationRow.updated_at.desc(),
                ConversationRow.conversation_id.desc(),
            )
        )
        try:
            rows = self._session.scalars(statement).all()
        except SQLAlchemyError as error:
            _raise_safe_persistence_error(error)
        return [_conversation_from_row(row) for row in rows]

    def add(self, conversation: Conversation) -> None:
        try:
            self._session.flush()
            self._session.add(
                ConversationRow(
                    conversation_id=conversation.conversation_id,
                    actor_id=conversation.actor_id,
                    title=conversation.title,
                    created_at=conversation.created_at,
                    updated_at=conversation.updated_at,
                )
            )
        except SQLAlchemyError as error:
            _raise_safe_persistence_error(error)

    def update(self, conversation: Conversation) -> Conversation | None:
        try:
            row = self._session.get(
                ConversationRow,
                conversation.conversation_id,
                populate_existing=True,
                with_for_update=True,
            )
            if row is None:
                return None
            if (
                row.actor_id != conversation.actor_id
                or row.created_at != conversation.created_at
            ):
                return None
            row.updated_at = max(row.updated_at, conversation.updated_at)
            if row.title is None and conversation.title is not None:
                row.title = conversation.title
            return _conversation_from_row(row)
        except SQLAlchemyError as error:
            _raise_safe_persistence_error(error)


class SQLAlchemyMessageRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def get(self, message_id: str) -> Message | None:
        try:
            row = self._session.get(MessageRow, message_id)
        except SQLAlchemyError as error:
            _raise_safe_persistence_error(error)
        return None if row is None else _message_from_row(row)

    def get_by_llm_call_id(self, llm_call_id: str) -> Message | None:
        statement = select(MessageRow).where(
            MessageRow.llm_call_id == llm_call_id
        )
        try:
            row = self._session.scalar(statement)
        except SQLAlchemyError as error:
            _raise_safe_persistence_error(error)
        return None if row is None else _message_from_row(row)

    def get_latest_for_conversation(
        self,
        conversation_id: str,
        actor_id: str,
    ) -> Message | None:
        statement = (
            select(MessageRow)
            .where(
                MessageRow.conversation_id == conversation_id,
                MessageRow.actor_id == actor_id,
            )
            .order_by(
                MessageRow.created_at.desc(),
                MessageRow.message_id.desc(),
            )
            .limit(1)
        )
        try:
            row = self._session.scalar(statement)
        except SQLAlchemyError as error:
            _raise_safe_persistence_error(error)
        return None if row is None else _message_from_row(row)

    def list_for_task(self, task_id: str) -> list[Message]:
        statement = (
            select(MessageRow)
            .where(MessageRow.task_id == task_id)
            .order_by(
                MessageRow.created_at.asc(),
                MessageRow.message_id.asc(),
            )
        )
        try:
            rows = self._session.scalars(statement).all()
        except SQLAlchemyError as error:
            _raise_safe_persistence_error(error)
        return [_message_from_row(row) for row in rows]

    def add(self, message: Message) -> None:
        try:
            self._session.flush()
            self._session.add(
                MessageRow(
                    message_id=message.message_id,
                    conversation_id=message.conversation_id,
                    task_id=message.task_id,
                    actor_id=message.actor_id,
                    request_id=message.request_id,
                    role=message.role,
                    generation_source=message.generation_source,
                    content_text=message.content_text,
                    structured_content=message.structured_content,
                    llm_call_id=message.llm_call_id,
                    created_at=message.created_at,
                )
            )
        except SQLAlchemyError as error:
            _raise_safe_persistence_error(error)


TASK_ALLOWED_TRANSITIONS: Final = {
    "PENDING": frozenset({"RUNNING", "FAILED"}),
    "RUNNING": frozenset({"NEEDS_INPUT", "SUCCEEDED", "FAILED"}),
    "NEEDS_INPUT": frozenset(),
    "SUCCEEDED": frozenset(),
    "PARTIALLY_SUCCEEDED": frozenset(),
    "FAILED": frozenset(),
}


class SQLAlchemyTaskRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def get(self, task_id: str) -> Task | None:
        try:
            row = self._session.get(TaskRow, task_id)
        except SQLAlchemyError as error:
            _raise_safe_persistence_error(error)
        return None if row is None else _task_from_row(row)

    def get_owned(self, task_id: str, actor_id: str) -> Task | None:
        statement = select(TaskRow).where(
            TaskRow.task_id == task_id,
            TaskRow.actor_id == actor_id,
        )
        try:
            row = self._session.scalar(statement)
        except SQLAlchemyError as error:
            _raise_safe_persistence_error(error)
        return None if row is None else _task_from_row(row)

    def add(self, task: Task) -> None:
        try:
            self._session.flush()
            self._session.add(
                TaskRow(
                    task_id=task.task_id,
                    conversation_id=task.conversation_id,
                    actor_id=task.actor_id,
                    task_type=task.task_type,
                    current_status=task.current_status,
                    selected_tool_run_id=task.selected_tool_run_id,
                    selected_result_id=task.selected_result_id,
                    created_at=task.created_at,
                    started_at=task.started_at,
                    updated_at=task.updated_at,
                    completed_at=task.completed_at,
                    error_code=task.error_code,
                    safe_error_message=task.safe_error_message,
                )
            )
        except SQLAlchemyError as error:
            _raise_safe_persistence_error(error)

    def update(
        self,
        task: Task,
        *,
        expected_status: str,
    ) -> Task | None:
        try:
            row = self._session.get(
                TaskRow,
                task.task_id,
                populate_existing=True,
                with_for_update=True,
            )
            if row is None or row.current_status != expected_status:
                return None
            if (
                row.conversation_id != task.conversation_id
                or row.actor_id != task.actor_id
                or row.created_at != task.created_at
                or task.current_status
                not in TASK_ALLOWED_TRANSITIONS.get(
                    row.current_status,
                    frozenset(),
                )
            ):
                return None
            row.task_type = task.task_type
            row.current_status = task.current_status
            row.selected_tool_run_id = task.selected_tool_run_id
            row.selected_result_id = task.selected_result_id
            row.started_at = task.started_at
            row.updated_at = task.updated_at
            row.completed_at = task.completed_at
            row.error_code = task.error_code
            row.safe_error_message = task.safe_error_message
            return _task_from_row(row)
        except SQLAlchemyError as error:
            _raise_safe_persistence_error(error)


class SQLAlchemyTaskInputRevisionRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def get(
        self,
        task_input_revision_id: str,
    ) -> TaskInputRevision | None:
        try:
            row = self._session.get(
                TaskInputRevisionRow,
                task_input_revision_id,
            )
        except SQLAlchemyError as error:
            _raise_safe_persistence_error(error)
        return None if row is None else _revision_from_row(row)

    def add(self, revision: TaskInputRevision) -> None:
        try:
            self._session.flush()
            self._session.add(
                TaskInputRevisionRow(
                    task_input_revision_id=revision.task_input_revision_id,
                    task_id=revision.task_id,
                    request_id=revision.request_id,
                    source_llm_call_id=revision.source_llm_call_id,
                    source_message_ids=list(revision.source_message_ids),
                    revision=revision.revision,
                    raw_input=revision.raw_input,
                    normalized_input=revision.normalized_input,
                    missing_fields=list(revision.missing_fields),
                    ambiguous_fields=list(revision.ambiguous_fields),
                    validation_errors=list(revision.validation_errors),
                    created_at=revision.created_at,
                )
            )
        except SQLAlchemyError as error:
            _raise_safe_persistence_error(error)

    def list_for_task(self, task_id: str) -> list[TaskInputRevision]:
        statement = (
            select(TaskInputRevisionRow)
            .where(TaskInputRevisionRow.task_id == task_id)
            .order_by(
                TaskInputRevisionRow.revision.asc(),
                TaskInputRevisionRow.task_input_revision_id.asc(),
            )
        )
        try:
            rows = self._session.scalars(statement).all()
        except SQLAlchemyError as error:
            _raise_safe_persistence_error(error)
        return [_revision_from_row(row) for row in rows]

    def list_for_llm_call_id(
        self,
        llm_call_id: str,
    ) -> list[TaskInputRevision]:
        statement = (
            select(TaskInputRevisionRow)
            .where(TaskInputRevisionRow.source_llm_call_id == llm_call_id)
            .order_by(
                TaskInputRevisionRow.task_id.asc(),
                TaskInputRevisionRow.revision.asc(),
                TaskInputRevisionRow.task_input_revision_id.asc(),
            )
        )
        try:
            rows = self._session.scalars(statement).all()
        except SQLAlchemyError as error:
            _raise_safe_persistence_error(error)
        return [_revision_from_row(row) for row in rows]
