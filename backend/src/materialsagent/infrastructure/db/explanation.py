from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    select,
)
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Mapped, Session, mapped_column

from materialsagent.domain.models.explanation import NaturalLanguageExplanation
from materialsagent.infrastructure.db.actor import _raise_safe_persistence_error
from materialsagent.infrastructure.db.base import Base


class NaturalLanguageExplanationRow(Base):
    __tablename__ = "natural_language_explanation"
    __table_args__ = (
        CheckConstraint(
            "length(btrim(explanation_id)) > 0",
            name="ck_explanation_id_not_blank",
        ),
        CheckConstraint(
            "length(btrim(task_id)) > 0",
            name="ck_explanation_task_id_not_blank",
        ),
        CheckConstraint(
            "length(btrim(result_id)) > 0",
            name="ck_explanation_result_id_not_blank",
        ),
        CheckConstraint(
            "length(btrim(llm_call_id)) > 0",
            name="ck_explanation_llm_call_id_not_blank",
        ),
        CheckConstraint(
            "attempt_no > 0",
            name="ck_explanation_attempt_positive",
        ),
        CheckConstraint(
            "status IN ('PENDING', 'RUNNING', 'SUCCEEDED', 'FAILED')",
            name="ck_explanation_status_allowed",
        ),
        CheckConstraint(
            "length(btrim(language)) > 0",
            name="ck_explanation_language_not_blank",
        ),
        CheckConstraint(
            "error_code IS NULL OR "
            "(length(btrim(error_code)) > 0 AND length(error_code) <= 64)",
            name="ck_explanation_error_code_safe",
        ),
        CheckConstraint(
            "safe_error_message IS NULL OR "
            "(length(btrim(safe_error_message)) > 0 "
            "AND length(safe_error_message) <= 256)",
            name="ck_explanation_safe_error_message_safe",
        ),
        CheckConstraint(
            "duration_ms IS NULL OR duration_ms >= 0",
            name="ck_explanation_duration_nonnegative",
        ),
        CheckConstraint(
            "started_at IS NULL OR started_at >= created_at",
            name="ck_explanation_started_not_before_created",
        ),
        CheckConstraint(
            "completed_at IS NULL OR "
            "(started_at IS NOT NULL AND completed_at >= started_at)",
            name="ck_explanation_completed_not_before_started",
        ),
        CheckConstraint(
            "(status = 'PENDING' AND started_at IS NULL "
            "AND completed_at IS NULL AND duration_ms IS NULL "
            "AND text IS NULL AND error_code IS NULL "
            "AND safe_error_message IS NULL) OR "
            "(status = 'RUNNING' AND started_at IS NOT NULL "
            "AND completed_at IS NULL AND duration_ms IS NULL "
            "AND text IS NULL AND error_code IS NULL "
            "AND safe_error_message IS NULL) OR "
            "(status = 'SUCCEEDED' AND started_at IS NOT NULL "
            "AND completed_at IS NOT NULL AND duration_ms IS NOT NULL "
            "AND text IS NOT NULL AND length(btrim(text)) > 0 "
            "AND length(text) <= 4096 "
            "AND error_code IS NULL AND safe_error_message IS NULL) OR "
            "(status = 'FAILED' AND started_at IS NOT NULL "
            "AND completed_at IS NOT NULL AND duration_ms IS NOT NULL "
            "AND text IS NULL AND error_code IS NOT NULL "
            "AND safe_error_message IS NOT NULL)",
            name="ck_explanation_status_shape",
        ),
        UniqueConstraint(
            "result_id",
            "attempt_no",
            name="uq_explanation_result_attempt",
        ),
        UniqueConstraint(
            "llm_call_id",
            name="uq_explanation_llm_call_id",
        ),
    )

    explanation_id: Mapped[str] = mapped_column(Text, primary_key=True)
    task_id: Mapped[str] = mapped_column(
        ForeignKey(
            "task.task_id",
            name="fk_explanation_task",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )
    result_id: Mapped[str] = mapped_column(
        ForeignKey(
            "tool_result.result_id",
            name="fk_explanation_result",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )
    llm_call_id: Mapped[str] = mapped_column(
        ForeignKey(
            "llm_call.llm_call_id",
            name="fk_explanation_llm_call",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )
    attempt_no: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    language: Mapped[str] = mapped_column(String(32), nullable=False)
    text: Mapped[str | None] = mapped_column(Text, nullable=True)
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
    error_code: Mapped[str | None] = mapped_column(String(128), nullable=True)
    safe_error_message: Mapped[str | None] = mapped_column(Text, nullable=True)


def _from_row(row: NaturalLanguageExplanationRow) -> NaturalLanguageExplanation:
    return NaturalLanguageExplanation(
        **{
            field: getattr(row, field)
            for field in NaturalLanguageExplanation.__dataclass_fields__
        }
    )


class SQLAlchemyExplanationRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def get(
        self,
        explanation_id: str,
    ) -> NaturalLanguageExplanation | None:
        try:
            row = self._session.get(
                NaturalLanguageExplanationRow,
                explanation_id,
            )
        except SQLAlchemyError as error:
            _raise_safe_persistence_error(error)
        return None if row is None else _from_row(row)

    def get_for_result_attempt(
        self,
        result_id: str,
        attempt_no: int,
    ) -> NaturalLanguageExplanation | None:
        statement = select(NaturalLanguageExplanationRow).where(
            NaturalLanguageExplanationRow.result_id == result_id,
            NaturalLanguageExplanationRow.attempt_no == attempt_no,
        )
        try:
            row = self._session.scalar(statement)
        except SQLAlchemyError as error:
            _raise_safe_persistence_error(error)
        return None if row is None else _from_row(row)

    def list_for_result(
        self,
        result_id: str,
    ) -> list[NaturalLanguageExplanation]:
        statement = (
            select(NaturalLanguageExplanationRow)
            .where(NaturalLanguageExplanationRow.result_id == result_id)
            .order_by(
                NaturalLanguageExplanationRow.attempt_no,
                NaturalLanguageExplanationRow.created_at,
            )
        )
        try:
            rows = self._session.scalars(statement).all()
        except SQLAlchemyError as error:
            _raise_safe_persistence_error(error)
        return [_from_row(row) for row in rows]

    def add(self, explanation: NaturalLanguageExplanation) -> None:
        try:
            self._session.add(
                NaturalLanguageExplanationRow(
                    **{
                        field: getattr(explanation, field)
                        for field in NaturalLanguageExplanation.__dataclass_fields__
                    }
                )
            )
            self._session.flush()
        except SQLAlchemyError as error:
            _raise_safe_persistence_error(error)

    def update(
        self,
        explanation: NaturalLanguageExplanation,
        *,
        expected_status: str,
    ) -> NaturalLanguageExplanation | None:
        try:
            row = self._session.get(
                NaturalLanguageExplanationRow,
                explanation.explanation_id,
                populate_existing=True,
                with_for_update=True,
            )
            if row is None or row.status != expected_status:
                return None
            immutable = (
                "task_id",
                "result_id",
                "llm_call_id",
                "attempt_no",
                "language",
                "created_at",
            )
            if any(
                getattr(row, field) != getattr(explanation, field)
                for field in immutable
            ):
                return None
            allowed = {
                "PENDING": {"RUNNING"},
                "RUNNING": {"SUCCEEDED", "FAILED"},
            }
            if explanation.status not in allowed.get(row.status, set()):
                return None
            for field in (
                "status",
                "text",
                "started_at",
                "completed_at",
                "duration_ms",
                "error_code",
                "safe_error_message",
            ):
                setattr(row, field, getattr(explanation, field))
            self._session.flush()
            return _from_row(row)
        except SQLAlchemyError as error:
            _raise_safe_persistence_error(error)
