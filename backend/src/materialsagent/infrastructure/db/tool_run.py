from __future__ import annotations

from datetime import datetime

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

from materialsagent.domain.models.tool_run import ToolRun
from materialsagent.infrastructure.db.actor import _raise_safe_persistence_error
from materialsagent.infrastructure.db.base import Base
from materialsagent.infrastructure.db.conversation_task import TaskRow


class ToolRunRow(Base):
    __tablename__ = "tool_run"
    __table_args__ = (
        CheckConstraint("length(btrim(tool_run_id)) > 0", name="ck_tool_run_id_not_blank"),
        CheckConstraint("length(btrim(task_id)) > 0", name="ck_tool_run_task_id_not_blank"),
        CheckConstraint("length(btrim(request_id)) > 0", name="ck_tool_run_request_id_not_blank"),
        CheckConstraint("length(btrim(task_input_revision_id)) > 0", name="ck_tool_run_revision_id_not_blank"),
        CheckConstraint("attempt_no > 0", name="ck_tool_run_attempt_positive"),
        CheckConstraint("length(btrim(tool_id)) > 0", name="ck_tool_run_tool_id_not_blank"),
        CheckConstraint("length(btrim(tool_version)) > 0", name="ck_tool_run_tool_version_not_blank"),
        CheckConstraint("length(btrim(schema_version)) > 0", name="ck_tool_run_schema_version_not_blank"),
        CheckConstraint("cardinality(requested_outputs) > 0", name="ck_tool_run_requested_outputs_nonempty"),
        CheckConstraint("completed_outputs <@ requested_outputs", name="ck_tool_run_completed_subset"),
        CheckConstraint("failed_outputs <@ requested_outputs", name="ck_tool_run_failed_subset"),
        CheckConstraint("NOT (completed_outputs && failed_outputs)", name="ck_tool_run_output_sets_disjoint"),
        CheckConstraint("jsonb_typeof(execution_input) = 'object'", name="ck_tool_run_execution_input_object"),
        CheckConstraint("actual_runtime_parameters IS NULL OR jsonb_typeof(actual_runtime_parameters) = 'object'", name="ck_tool_run_runtime_parameters_object"),
        CheckConstraint("jsonb_typeof(diagnostics) = 'array'", name="ck_tool_run_diagnostics_array"),
        CheckConstraint("output_summary IS NULL OR jsonb_typeof(output_summary) = 'object'", name="ck_tool_run_output_summary_object"),
        CheckConstraint("current_status IN ('PENDING', 'RUNNING', 'SUCCEEDED', 'PARTIALLY_SUCCEEDED', 'FAILED')", name="ck_tool_run_status_allowed"),
        CheckConstraint("model_bundle_id IS NULL OR length(btrim(model_bundle_id)) > 0", name="ck_tool_run_model_bundle_not_blank"),
        CheckConstraint("started_at IS NULL OR started_at >= created_at", name="ck_tool_run_started_not_before_created"),
        CheckConstraint("completed_at IS NULL OR (started_at IS NOT NULL AND completed_at >= started_at)", name="ck_tool_run_completed_not_before_started"),
        CheckConstraint("duration_ms IS NULL OR duration_ms >= 0", name="ck_tool_run_duration_nonnegative"),
        CheckConstraint("error_code IS NULL OR length(btrim(error_code)) > 0", name="ck_tool_run_error_code_not_blank"),
        CheckConstraint("safe_error_message IS NULL OR length(btrim(safe_error_message)) > 0", name="ck_tool_run_safe_error_not_blank"),
        CheckConstraint(
            "(current_status = 'PENDING' AND started_at IS NULL AND completed_at IS NULL AND duration_ms IS NULL AND actual_runtime_parameters IS NULL AND output_summary IS NULL) OR "
            "(current_status = 'RUNNING' AND started_at IS NOT NULL AND completed_at IS NULL AND duration_ms IS NULL) OR "
            "(current_status IN ('SUCCEEDED', 'PARTIALLY_SUCCEEDED', 'FAILED') AND started_at IS NOT NULL AND completed_at IS NOT NULL AND duration_ms IS NOT NULL)",
            name="ck_tool_run_status_time_shape",
        ),
        CheckConstraint("current_status <> 'FAILED' OR (cardinality(completed_outputs) = 0 AND error_code IS NOT NULL)", name="ck_tool_run_failed_shape"),
        UniqueConstraint("task_id", "attempt_no", name="uq_tool_run_task_attempt"),
    )

    tool_run_id: Mapped[str] = mapped_column(Text, primary_key=True)
    task_id: Mapped[str] = mapped_column(ForeignKey("task.task_id", name="fk_tool_run_task", ondelete="RESTRICT"), nullable=False)
    request_id: Mapped[str] = mapped_column(Text, nullable=False)
    task_input_revision_id: Mapped[str] = mapped_column(ForeignKey("task_input_revision.task_input_revision_id", name="fk_tool_run_revision", ondelete="RESTRICT"), nullable=False)
    attempt_no: Mapped[int] = mapped_column(Integer, nullable=False)
    tool_id: Mapped[str] = mapped_column(Text, nullable=False)
    tool_version: Mapped[str] = mapped_column(String(64), nullable=False)
    schema_version: Mapped[str] = mapped_column(String(64), nullable=False)
    requested_outputs: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False)
    completed_outputs: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False)
    failed_outputs: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False)
    execution_input: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    actual_runtime_parameters: Mapped[dict[str, object] | None] = mapped_column(JSONB(none_as_null=True), nullable=True)
    diagnostics: Mapped[list[dict[str, object]]] = mapped_column(JSONB, nullable=False)
    output_summary: Mapped[dict[str, object] | None] = mapped_column(JSONB(none_as_null=True), nullable=True)
    current_status: Mapped[str] = mapped_column(String(32), nullable=False)
    model_bundle_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(128), nullable=True)
    safe_error_message: Mapped[str | None] = mapped_column(Text, nullable=True)


def _from_row(row: ToolRunRow) -> ToolRun:
    return ToolRun(
        tool_run_id=row.tool_run_id,
        task_id=row.task_id,
        request_id=row.request_id,
        task_input_revision_id=row.task_input_revision_id,
        attempt_no=row.attempt_no,
        tool_id=row.tool_id,
        tool_version=row.tool_version,
        schema_version=row.schema_version,
        requested_outputs=list(row.requested_outputs),
        completed_outputs=list(row.completed_outputs),
        failed_outputs=list(row.failed_outputs),
        execution_input=dict(row.execution_input),
        actual_runtime_parameters=(dict(row.actual_runtime_parameters) if row.actual_runtime_parameters is not None else None),
        diagnostics=[dict(item) for item in row.diagnostics],
        output_summary=(dict(row.output_summary) if row.output_summary is not None else None),
        current_status=row.current_status,
        model_bundle_id=row.model_bundle_id,
        created_at=row.created_at,
        started_at=row.started_at,
        completed_at=row.completed_at,
        duration_ms=row.duration_ms,
        error_code=row.error_code,
        safe_error_message=row.safe_error_message,
    )


class SQLAlchemyToolRunRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def get(self, tool_run_id: str) -> ToolRun | None:
        try:
            row = self._session.get(ToolRunRow, tool_run_id)
        except SQLAlchemyError as error:
            _raise_safe_persistence_error(error)
        return None if row is None else _from_row(row)

    def get_owned(self, tool_run_id: str, actor_id: str) -> ToolRun | None:
        statement = select(ToolRunRow).join(TaskRow, TaskRow.task_id == ToolRunRow.task_id).where(
            ToolRunRow.tool_run_id == tool_run_id,
            TaskRow.actor_id == actor_id,
        )
        try:
            row = self._session.scalar(statement)
        except SQLAlchemyError as error:
            _raise_safe_persistence_error(error)
        return None if row is None else _from_row(row)

    def get_owned_for_update(
        self,
        tool_run_id: str,
        actor_id: str,
    ) -> ToolRun | None:
        statement = (
            select(ToolRunRow)
            .join(TaskRow, TaskRow.task_id == ToolRunRow.task_id)
            .where(
                ToolRunRow.tool_run_id == tool_run_id,
                TaskRow.actor_id == actor_id,
            )
            .with_for_update()
        )
        try:
            row = self._session.scalar(statement)
        except SQLAlchemyError as error:
            _raise_safe_persistence_error(error)
        return None if row is None else _from_row(row)

    def list_for_task(self, task_id: str) -> list[ToolRun]:
        statement = select(ToolRunRow).where(ToolRunRow.task_id == task_id).order_by(ToolRunRow.attempt_no, ToolRunRow.tool_run_id)
        try:
            rows = self._session.scalars(statement).all()
        except SQLAlchemyError as error:
            _raise_safe_persistence_error(error)
        return [_from_row(row) for row in rows]

    def add(self, tool_run: ToolRun) -> None:
        try:
            self._session.flush()
            self._session.add(ToolRunRow(**{field: getattr(tool_run, field) for field in ToolRun.__dataclass_fields__}))
            self._session.flush()
        except SQLAlchemyError as error:
            _raise_safe_persistence_error(error)

    def update(self, tool_run: ToolRun, *, expected_status: str) -> ToolRun | None:
        try:
            row = self._session.get(ToolRunRow, tool_run.tool_run_id, populate_existing=True, with_for_update=True)
            if row is None or row.current_status != expected_status:
                return None
            immutable = (
                "task_id", "request_id", "task_input_revision_id", "attempt_no",
                "tool_id", "tool_version", "schema_version", "requested_outputs",
                "execution_input", "created_at",
            )
            if any(getattr(row, field) != getattr(tool_run, field) for field in immutable):
                return None
            allowed = {
                "PENDING": {"RUNNING"},
                "RUNNING": {"RUNNING", "SUCCEEDED", "PARTIALLY_SUCCEEDED", "FAILED"},
            }
            if tool_run.current_status not in allowed.get(row.current_status, set()):
                return None
            for field in (
                "completed_outputs", "failed_outputs", "actual_runtime_parameters",
                "diagnostics", "output_summary", "current_status", "model_bundle_id",
                "started_at", "completed_at", "duration_ms", "error_code",
                "safe_error_message",
            ):
                setattr(row, field, getattr(tool_run, field))
            self._session.flush()
            return _from_row(row)
        except SQLAlchemyError as error:
            _raise_safe_persistence_error(error)
