from __future__ import annotations

from dataclasses import replace
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
    select,
)
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Mapped, Session, mapped_column

from materialsagent.domain.models.idempotency_record import IdempotencyRecord
from materialsagent.infrastructure.db.actor import _raise_safe_persistence_error
from materialsagent.infrastructure.db.base import Base


class IdempotencyRecordRow(Base):
    __tablename__ = "idempotency_record"
    __table_args__ = (
        CheckConstraint(
            "length(btrim(idempotency_record_id)) > 0",
            name="ck_idempotency_record_id_not_blank",
        ),
        CheckConstraint(
            "length(btrim(actor_id)) > 0",
            name="ck_idempotency_actor_id_not_blank",
        ),
        CheckConstraint(
            "operation IN ('TASK_CREATE', 'TASK_INPUT_SUPPLEMENT', "
            "'TOOL_RETRY', 'EXPLANATION_RETRY')",
            name="ck_idempotency_operation_allowed",
        ),
        CheckConstraint(
            "length(idempotency_key) BETWEEN 1 AND 255 "
            "AND length(btrim(idempotency_key)) > 0 "
            "AND idempotency_key !~ '[[:cntrl:]]'",
            name="ck_idempotency_key_safe",
        ),
        CheckConstraint(
            "request_digest ~ '^[0-9a-f]{64}$'",
            name="ck_idempotency_digest_sha256",
        ),
        CheckConstraint(
            "length(btrim(first_request_id)) > 0",
            name="ck_idempotency_first_request_not_blank",
        ),
        CheckConstraint(
            "(operation = 'TASK_CREATE' AND task_id IS NOT NULL "
            "AND message_id IS NOT NULL AND task_input_revision_id IS NULL "
            "AND tool_run_id IS NULL AND explanation_id IS NULL) OR "
            "(operation = 'TASK_INPUT_SUPPLEMENT' AND task_id IS NOT NULL "
            "AND message_id IS NOT NULL AND tool_run_id IS NULL "
            "AND explanation_id IS NULL) OR "
            "(operation = 'TOOL_RETRY' AND task_id IS NOT NULL "
            "AND message_id IS NULL AND task_input_revision_id IS NULL "
            "AND tool_run_id IS NOT NULL AND explanation_id IS NULL) OR "
            "(operation = 'EXPLANATION_RETRY' AND task_id IS NOT NULL "
            "AND message_id IS NULL AND task_input_revision_id IS NULL "
            "AND tool_run_id IS NULL AND explanation_id IS NOT NULL)",
            name="ck_idempotency_operation_binding",
        ),
        UniqueConstraint(
            "actor_id",
            "operation",
            "idempotency_key",
            name="uq_idempotency_scope_key",
        ),
        UniqueConstraint(
            "first_request_id",
            name="uq_idempotency_first_request",
        ),
        Index(
            "ix_idempotency_scope_created",
            "actor_id",
            "operation",
            "created_at",
            "idempotency_record_id",
        ),
        Index(
            "ix_idempotency_task_created",
            "task_id",
            "created_at",
            "idempotency_record_id",
        ),
    )

    idempotency_record_id: Mapped[str] = mapped_column(Text, primary_key=True)
    actor_id: Mapped[str] = mapped_column(
        ForeignKey(
            "actor.actor_id",
            name="fk_idempotency_actor",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )
    operation: Mapped[str] = mapped_column(String(32), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    request_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    first_request_id: Mapped[str] = mapped_column(Text, nullable=False)
    task_id: Mapped[str | None] = mapped_column(
        ForeignKey(
            "task.task_id",
            name="fk_idempotency_task",
            ondelete="RESTRICT",
        ),
        nullable=True,
    )
    message_id: Mapped[str | None] = mapped_column(
        ForeignKey(
            "message.message_id",
            name="fk_idempotency_message",
            ondelete="RESTRICT",
        ),
        nullable=True,
    )
    task_input_revision_id: Mapped[str | None] = mapped_column(
        ForeignKey(
            "task_input_revision.task_input_revision_id",
            name="fk_idempotency_revision",
            ondelete="RESTRICT",
        ),
        nullable=True,
    )
    tool_run_id: Mapped[str | None] = mapped_column(
        ForeignKey(
            "tool_run.tool_run_id",
            name="fk_idempotency_tool_run",
            ondelete="RESTRICT",
        ),
        nullable=True,
    )
    explanation_id: Mapped[str | None] = mapped_column(
        ForeignKey(
            "natural_language_explanation.explanation_id",
            name="fk_idempotency_explanation",
            ondelete="RESTRICT",
        ),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )
    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )


def _from_row(row: IdempotencyRecordRow) -> IdempotencyRecord:
    return IdempotencyRecord(
        **{
            field_name: getattr(row, field_name)
            for field_name in IdempotencyRecord.__dataclass_fields__
        }
    )


class SQLAlchemyIdempotencyRecordRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def get_by_scope(
        self,
        actor_id: str,
        operation: str,
        idempotency_key: str,
    ) -> IdempotencyRecord | None:
        statement = select(IdempotencyRecordRow).where(
            IdempotencyRecordRow.actor_id == actor_id,
            IdempotencyRecordRow.operation == operation,
            IdempotencyRecordRow.idempotency_key == idempotency_key,
        )
        try:
            row = self._session.scalar(statement)
        except SQLAlchemyError as error:
            _raise_safe_persistence_error(error)
        return None if row is None else _from_row(row)

    def get_by_first_request_id(
        self,
        first_request_id: str,
    ) -> IdempotencyRecord | None:
        statement = select(IdempotencyRecordRow).where(
            IdempotencyRecordRow.first_request_id == first_request_id
        )
        try:
            row = self._session.scalar(statement)
        except SQLAlchemyError as error:
            _raise_safe_persistence_error(error)
        return None if row is None else _from_row(row)

    def get_unbound_supplement_for_task(
        self,
        task_id: str,
    ) -> IdempotencyRecord | None:
        statement = (
            select(IdempotencyRecordRow)
            .where(
                IdempotencyRecordRow.operation == "TASK_INPUT_SUPPLEMENT",
                IdempotencyRecordRow.task_id == task_id,
                IdempotencyRecordRow.task_input_revision_id.is_(None),
            )
            .order_by(
                IdempotencyRecordRow.created_at,
                IdempotencyRecordRow.idempotency_record_id,
            )
            .limit(1)
        )
        try:
            row = self._session.scalar(statement)
        except SQLAlchemyError as error:
            _raise_safe_persistence_error(error)
        return None if row is None else _from_row(row)

    def add(self, record: IdempotencyRecord) -> None:
        try:
            self._session.add(
                IdempotencyRecordRow(
                    **{
                        field_name: getattr(record, field_name)
                        for field_name in IdempotencyRecord.__dataclass_fields__
                    }
                )
            )
            self._session.flush()
        except SQLAlchemyError as error:
            _raise_safe_persistence_error(error)

    def bind_task_input_revision(
        self,
        record: IdempotencyRecord,
        task_input_revision_id: str,
    ) -> IdempotencyRecord | None:
        if record.operation != "TASK_INPUT_SUPPLEMENT":
            return None
        updated = replace(
            record,
            task_input_revision_id=task_input_revision_id,
        )
        try:
            row = self._session.get(
                IdempotencyRecordRow,
                record.idempotency_record_id,
                populate_existing=True,
                with_for_update=True,
            )
            if row is None:
                return None
            current = _from_row(row)
            if current == updated:
                return current
            if current != record or row.task_input_revision_id is not None:
                return None
            row.task_input_revision_id = task_input_revision_id
            self._session.flush()
            return _from_row(row)
        except SQLAlchemyError as error:
            _raise_safe_persistence_error(error)
