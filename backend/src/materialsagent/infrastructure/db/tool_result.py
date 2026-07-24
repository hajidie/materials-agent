from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    select,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Mapped, Session, mapped_column

from materialsagent.domain.models.result_asset_link import ResultAssetLink
from materialsagent.domain.models.tool_result import ToolResult
from materialsagent.infrastructure.db.actor import _raise_safe_persistence_error
from materialsagent.infrastructure.db.base import Base
from materialsagent.infrastructure.db.conversation_task import TaskRow


def _plain_json(value: object) -> object:
    if isinstance(value, Mapping):
        return {key: _plain_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_plain_json(item) for item in value]
    return value


class ToolResultRow(Base):
    __tablename__ = "tool_result"
    __table_args__ = (
        CheckConstraint(
            "length(btrim(result_id)) > 0",
            name="ck_tool_result_id_not_blank",
        ),
        CheckConstraint(
            "length(btrim(task_id)) > 0",
            name="ck_tool_result_task_id_not_blank",
        ),
        CheckConstraint(
            "length(btrim(tool_run_id)) > 0",
            name="ck_tool_result_tool_run_id_not_blank",
        ),
        CheckConstraint(
            "length(btrim(actor_id)) > 0",
            name="ck_tool_result_actor_id_not_blank",
        ),
        CheckConstraint(
            "length(btrim(tool_id)) > 0",
            name="ck_tool_result_tool_id_not_blank",
        ),
        CheckConstraint(
            "length(btrim(tool_version)) > 0",
            name="ck_tool_result_tool_version_not_blank",
        ),
        CheckConstraint(
            "length(btrim(schema_version)) > 0",
            name="ck_tool_result_schema_version_not_blank",
        ),
        CheckConstraint(
            "status IN ('SUCCEEDED', 'PARTIALLY_SUCCEEDED', 'FAILED')",
            name="ck_tool_result_status_allowed",
        ),
        CheckConstraint(
            "cardinality(requested_outputs) > 0",
            name="ck_tool_result_requested_nonempty",
        ),
        CheckConstraint(
            "completed_outputs <@ requested_outputs "
            "AND failed_outputs <@ requested_outputs "
            "AND NOT (completed_outputs && failed_outputs) "
            "AND requested_outputs <@ (completed_outputs || failed_outputs)",
            name="ck_tool_result_output_sets",
        ),
        CheckConstraint(
            "(status = 'SUCCEEDED' "
            "AND cardinality(completed_outputs) = cardinality(requested_outputs) "
            "AND cardinality(failed_outputs) = 0) OR "
            "(status = 'PARTIALLY_SUCCEEDED' "
            "AND cardinality(completed_outputs) > 0 "
            "AND cardinality(failed_outputs) > 0) OR "
            "(status = 'FAILED' "
            "AND cardinality(completed_outputs) = 0 "
            "AND cardinality(failed_outputs) = cardinality(requested_outputs))",
            name="ck_tool_result_status_shape",
        ),
        CheckConstraint(
            "jsonb_typeof(data) = 'object'",
            name="ck_tool_result_data_object",
        ),
        CheckConstraint(
            "jsonb_typeof(warnings) = 'array'",
            name="ck_tool_result_warnings_array",
        ),
        CheckConstraint(
            "jsonb_typeof(provenance) = 'object'",
            name="ck_tool_result_provenance_object",
        ),
        CheckConstraint(
            "error IS NULL OR jsonb_typeof(error) = 'object'",
            name="ck_tool_result_error_object",
        ),
        UniqueConstraint("tool_run_id", name="uq_tool_result_tool_run_id"),
        Index(
            "ix_tool_result_task_created",
            "task_id",
            "created_at",
            "result_id",
        ),
    )

    result_id: Mapped[str] = mapped_column(Text, primary_key=True)
    task_id: Mapped[str] = mapped_column(
        ForeignKey("task.task_id", name="fk_tool_result_task", ondelete="RESTRICT"),
        nullable=False,
    )
    tool_run_id: Mapped[str] = mapped_column(
        ForeignKey(
            "tool_run.tool_run_id",
            name="fk_tool_result_tool_run",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )
    actor_id: Mapped[str] = mapped_column(
        ForeignKey(
            "actor.actor_id",
            name="fk_tool_result_actor",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    requested_outputs: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False)
    completed_outputs: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False)
    failed_outputs: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False)
    data: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    warnings: Mapped[list[object]] = mapped_column(JSONB, nullable=False)
    provenance: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    error: Mapped[dict[str, object] | None] = mapped_column(
        JSONB(none_as_null=True),
        nullable=True,
    )
    tool_id: Mapped[str] = mapped_column(Text, nullable=False)
    tool_version: Mapped[str] = mapped_column(Text, nullable=False)
    schema_version: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )


class ResultAssetLinkRow(Base):
    __tablename__ = "result_asset_link"
    __table_args__ = (
        CheckConstraint(
            "length(btrim(result_id)) > 0",
            name="ck_result_asset_link_result_id_not_blank",
        ),
        CheckConstraint(
            "length(btrim(asset_id)) > 0",
            name="ck_result_asset_link_asset_id_not_blank",
        ),
        CheckConstraint(
            "artifact_order >= 0",
            name="ck_result_asset_link_order_nonnegative",
        ),
        UniqueConstraint(
            "result_id",
            "artifact_order",
            name="uq_result_asset_link_result_order",
        ),
    )

    result_id: Mapped[str] = mapped_column(
        ForeignKey(
            "tool_result.result_id",
            name="fk_result_asset_link_result",
            ondelete="RESTRICT",
        ),
        primary_key=True,
    )
    asset_id: Mapped[str] = mapped_column(
        ForeignKey(
            "asset.asset_id",
            name="fk_result_asset_link_asset",
            ondelete="RESTRICT",
        ),
        primary_key=True,
    )
    artifact_order: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )


def _result_from_row(row: ToolResultRow) -> ToolResult:
    return ToolResult(
        result_id=row.result_id,
        task_id=row.task_id,
        tool_run_id=row.tool_run_id,
        actor_id=row.actor_id,
        status=row.status,
        requested_outputs=list(row.requested_outputs),
        completed_outputs=list(row.completed_outputs),
        failed_outputs=list(row.failed_outputs),
        data=dict(row.data),
        warnings=list(row.warnings),
        provenance=dict(row.provenance),
        error=None if row.error is None else dict(row.error),
        tool_id=row.tool_id,
        tool_version=row.tool_version,
        schema_version=row.schema_version,
        created_at=row.created_at,
    )


def _link_from_row(row: ResultAssetLinkRow) -> ResultAssetLink:
    return ResultAssetLink(
        result_id=row.result_id,
        asset_id=row.asset_id,
        artifact_order=row.artifact_order,
        created_at=row.created_at,
    )


class SQLAlchemyToolResultRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def get(self, result_id: str) -> ToolResult | None:
        try:
            row = self._session.get(ToolResultRow, result_id)
        except SQLAlchemyError as error:
            _raise_safe_persistence_error(error)
        return None if row is None else _result_from_row(row)

    def get_owned(self, result_id: str, actor_id: str) -> ToolResult | None:
        statement = (
            select(ToolResultRow)
            .join(TaskRow, TaskRow.task_id == ToolResultRow.task_id)
            .where(
                ToolResultRow.result_id == result_id,
                ToolResultRow.actor_id == actor_id,
                TaskRow.actor_id == actor_id,
            )
        )
        try:
            row = self._session.scalar(statement)
        except SQLAlchemyError as error:
            _raise_safe_persistence_error(error)
        return None if row is None else _result_from_row(row)

    def get_owned_for_update(
        self,
        result_id: str,
        actor_id: str,
    ) -> ToolResult | None:
        statement = (
            select(ToolResultRow)
            .join(TaskRow, TaskRow.task_id == ToolResultRow.task_id)
            .where(
                ToolResultRow.result_id == result_id,
                ToolResultRow.actor_id == actor_id,
                TaskRow.actor_id == actor_id,
            )
            .with_for_update(of=ToolResultRow)
        )
        try:
            row = self._session.scalar(statement)
        except SQLAlchemyError as error:
            _raise_safe_persistence_error(error)
        return None if row is None else _result_from_row(row)

    def get_for_tool_run(self, tool_run_id: str) -> ToolResult | None:
        try:
            row = self._session.scalar(
                select(ToolResultRow).where(
                    ToolResultRow.tool_run_id == tool_run_id
                )
            )
        except SQLAlchemyError as error:
            _raise_safe_persistence_error(error)
        return None if row is None else _result_from_row(row)

    def add(self, result: ToolResult) -> None:
        try:
            self._session.add(
                ToolResultRow(
                    result_id=result.result_id,
                    task_id=result.task_id,
                    tool_run_id=result.tool_run_id,
                    actor_id=result.actor_id,
                    status=result.status,
                    requested_outputs=list(result.requested_outputs),
                    completed_outputs=list(result.completed_outputs),
                    failed_outputs=list(result.failed_outputs),
                    data=_plain_json(result.data),
                    warnings=_plain_json(result.warnings),
                    provenance=_plain_json(result.provenance),
                    error=(
                        None
                        if result.error is None
                        else _plain_json(result.error)
                    ),
                    tool_id=result.tool_id,
                    tool_version=result.tool_version,
                    schema_version=result.schema_version,
                    created_at=result.created_at,
                )
            )
            self._session.flush()
        except SQLAlchemyError as error:
            _raise_safe_persistence_error(error)


class SQLAlchemyResultAssetLinkRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def list_for_result(self, result_id: str) -> list[ResultAssetLink]:
        statement = (
            select(ResultAssetLinkRow)
            .where(ResultAssetLinkRow.result_id == result_id)
            .order_by(
                ResultAssetLinkRow.artifact_order,
                ResultAssetLinkRow.asset_id,
            )
        )
        try:
            rows = self._session.scalars(statement).all()
        except SQLAlchemyError as error:
            _raise_safe_persistence_error(error)
        return [_link_from_row(row) for row in rows]

    def add(self, link: ResultAssetLink) -> None:
        try:
            self._session.add(
                ResultAssetLinkRow(
                    result_id=link.result_id,
                    asset_id=link.asset_id,
                    artifact_order=link.artifact_order,
                    created_at=link.created_at,
                )
            )
            self._session.flush()
        except SQLAlchemyError as error:
            _raise_safe_persistence_error(error)
