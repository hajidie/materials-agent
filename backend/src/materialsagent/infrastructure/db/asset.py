from __future__ import annotations

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
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Mapped, Session, mapped_column

from materialsagent.domain.models.asset import Asset
from materialsagent.infrastructure.db.actor import _raise_safe_persistence_error
from materialsagent.infrastructure.db.base import Base
from materialsagent.infrastructure.db.conversation_task import TaskRow
from materialsagent.infrastructure.db.tool_run import ToolRunRow


class AssetRow(Base):
    __tablename__ = "asset"
    __table_args__ = (
        CheckConstraint("length(btrim(asset_id)) > 0", name="ck_asset_id_not_blank"),
        CheckConstraint("length(btrim(task_id)) > 0", name="ck_asset_task_id_not_blank"),
        CheckConstraint("length(btrim(producer_tool_run_id)) > 0", name="ck_asset_tool_run_id_not_blank"),
        CheckConstraint("length(btrim(actor_id)) > 0", name="ck_asset_actor_id_not_blank"),
        CheckConstraint("length(btrim(operation_id)) > 0", name="ck_asset_operation_id_not_blank"),
        CheckConstraint("current_status IN ('PENDING', 'AVAILABLE', 'FAILED', 'ORPHANED')", name="ck_asset_status_allowed"),
        CheckConstraint("asset_type = 'sem_image'", name="ck_asset_type_sem_image"),
        CheckConstraint("source_type = 'GENERATED' AND producer_tool_run_id IS NOT NULL", name="ck_asset_generated_source"),
        CheckConstraint("role IN ('requested_output', 'intermediate', 'supporting')", name="ck_asset_role_allowed"),
        CheckConstraint("length(btrim(object_key)) > 0", name="ck_asset_object_key_not_blank"),
        CheckConstraint("media_type IS NULL OR length(btrim(media_type)) > 0", name="ck_asset_media_type_not_blank"),
        CheckConstraint("width IS NULL OR width > 0", name="ck_asset_width_positive"),
        CheckConstraint("height IS NULL OR height > 0", name="ck_asset_height_positive"),
        CheckConstraint("bit_depth IS NULL OR bit_depth > 0", name="ck_asset_bit_depth_positive"),
        CheckConstraint("sha256 IS NULL OR sha256 ~ '^[0-9a-f]{64}$'", name="ck_asset_sha256_format"),
        CheckConstraint("size_bytes IS NULL OR size_bytes >= 0", name="ck_asset_size_nonnegative"),
        CheckConstraint("encoding_rule IS NULL OR length(btrim(encoding_rule)) > 0", name="ck_asset_encoding_rule_not_blank"),
        CheckConstraint("pending_since >= created_at", name="ck_asset_pending_not_before_created"),
        CheckConstraint("available_at IS NULL OR available_at >= created_at", name="ck_asset_available_not_before_created"),
        CheckConstraint("failed_at IS NULL OR failed_at >= created_at", name="ck_asset_failed_not_before_created"),
        CheckConstraint("orphaned_at IS NULL OR orphaned_at >= created_at", name="ck_asset_orphaned_not_before_created"),
        CheckConstraint("error_code IS NULL OR length(btrim(error_code)) > 0", name="ck_asset_error_code_not_blank"),
        CheckConstraint("safe_error_message IS NULL OR length(btrim(safe_error_message)) > 0", name="ck_asset_safe_error_not_blank"),
        CheckConstraint("orphan_reason IS NULL OR length(btrim(orphan_reason)) > 0", name="ck_asset_orphan_reason_not_blank"),
        CheckConstraint("orphan_details IS NULL OR jsonb_typeof(orphan_details) = 'object'", name="ck_asset_orphan_details_object"),
        CheckConstraint(
            "(current_status = 'PENDING' AND media_type IS NULL AND width IS NULL AND height IS NULL AND bit_depth IS NULL AND sha256 IS NULL AND size_bytes IS NULL AND encoding_rule IS NULL AND available_at IS NULL AND failed_at IS NULL AND orphaned_at IS NULL AND error_code IS NULL AND safe_error_message IS NULL AND orphan_reason IS NULL AND orphan_details IS NULL) OR "
            "(current_status = 'AVAILABLE' AND media_type IS NOT NULL AND width IS NOT NULL AND height IS NOT NULL AND bit_depth IS NOT NULL AND sha256 IS NOT NULL AND size_bytes IS NOT NULL AND encoding_rule IS NOT NULL AND available_at IS NOT NULL AND failed_at IS NULL AND orphaned_at IS NULL AND error_code IS NULL AND safe_error_message IS NULL AND orphan_reason IS NULL AND orphan_details IS NULL) OR "
            "(current_status = 'FAILED' AND media_type IS NULL AND width IS NULL AND height IS NULL AND bit_depth IS NULL AND sha256 IS NULL AND size_bytes IS NULL AND encoding_rule IS NULL AND available_at IS NULL AND failed_at IS NOT NULL AND orphaned_at IS NULL AND error_code IS NOT NULL AND safe_error_message IS NOT NULL AND orphan_reason IS NULL AND orphan_details IS NULL) OR "
            "(current_status = 'ORPHANED' AND failed_at IS NULL AND orphaned_at IS NOT NULL AND error_code IS NULL AND safe_error_message IS NULL AND orphan_reason IS NOT NULL AND orphan_details IS NOT NULL AND ((media_type IS NULL AND width IS NULL AND height IS NULL AND bit_depth IS NULL AND sha256 IS NULL AND size_bytes IS NULL AND encoding_rule IS NULL AND available_at IS NULL) OR (media_type IS NOT NULL AND width IS NOT NULL AND height IS NOT NULL AND bit_depth IS NOT NULL AND sha256 IS NOT NULL AND size_bytes IS NOT NULL AND encoding_rule IS NOT NULL AND available_at IS NOT NULL)))",
            name="ck_asset_status_shape",
        ),
        UniqueConstraint("object_key", name="uq_asset_object_key"),
        UniqueConstraint("operation_id", name="uq_asset_operation_id"),
        Index("ix_asset_tool_run_created", "producer_tool_run_id", "created_at", "asset_id"),
        Index("ix_asset_status_pending", "current_status", "pending_since"),
    )

    asset_id: Mapped[str] = mapped_column(Text, primary_key=True)
    task_id: Mapped[str] = mapped_column(ForeignKey("task.task_id", name="fk_asset_task", ondelete="RESTRICT"), nullable=False)
    producer_tool_run_id: Mapped[str] = mapped_column(ForeignKey("tool_run.tool_run_id", name="fk_asset_tool_run", ondelete="RESTRICT"), nullable=False)
    actor_id: Mapped[str] = mapped_column(ForeignKey("actor.actor_id", name="fk_asset_actor", ondelete="RESTRICT"), nullable=False)
    operation_id: Mapped[str] = mapped_column(Text, nullable=False)
    current_status: Mapped[str] = mapped_column(String(32), nullable=False)
    asset_type: Mapped[str] = mapped_column(String(64), nullable=False)
    source_type: Mapped[str] = mapped_column(String(32), nullable=False)
    role: Mapped[str] = mapped_column(String(64), nullable=False)
    object_key: Mapped[str] = mapped_column(Text, nullable=False)
    media_type: Mapped[str | None] = mapped_column(String(128), nullable=True)
    width: Mapped[int | None] = mapped_column(Integer, nullable=True)
    height: Mapped[int | None] = mapped_column(Integer, nullable=True)
    bit_depth: Mapped[int | None] = mapped_column(Integer, nullable=True)
    sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    size_bytes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    encoding_rule: Mapped[str | None] = mapped_column(Text, nullable=True)
    pending_since: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    available_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    failed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    orphaned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(128), nullable=True)
    safe_error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    orphan_reason: Mapped[str | None] = mapped_column(String(128), nullable=True)
    orphan_details: Mapped[dict[str, object] | None] = mapped_column(JSONB(none_as_null=True), nullable=True)


def _from_row(row: AssetRow) -> Asset:
    return Asset(
        **{
            field: (
                dict(row.orphan_details)
                if field == "orphan_details" and row.orphan_details is not None
                else getattr(row, field)
            )
            for field in Asset.__dataclass_fields__
        }
    )


class SQLAlchemyAssetRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def get(self, asset_id: str) -> Asset | None:
        try:
            row = self._session.get(AssetRow, asset_id)
        except SQLAlchemyError as error:
            _raise_safe_persistence_error(error)
        return None if row is None else _from_row(row)

    def get_owned(self, asset_id: str, actor_id: str) -> Asset | None:
        statement = (
            select(AssetRow)
            .join(ToolRunRow, ToolRunRow.tool_run_id == AssetRow.producer_tool_run_id)
            .join(TaskRow, TaskRow.task_id == ToolRunRow.task_id)
            .where(
                AssetRow.asset_id == asset_id,
                AssetRow.task_id == TaskRow.task_id,
                AssetRow.actor_id == TaskRow.actor_id,
                TaskRow.actor_id == actor_id,
            )
        )
        try:
            row = self._session.scalar(statement)
        except SQLAlchemyError as error:
            _raise_safe_persistence_error(error)
        return None if row is None else _from_row(row)

    def get_for_update(self, asset_id: str) -> Asset | None:
        statement = (
            select(AssetRow)
            .where(AssetRow.asset_id == asset_id)
            .with_for_update()
        )
        try:
            row = self._session.scalar(statement)
        except SQLAlchemyError as error:
            _raise_safe_persistence_error(error)
        return None if row is None else _from_row(row)

    def list_for_tool_run(self, tool_run_id: str) -> list[Asset]:
        statement = (
            select(AssetRow)
            .where(AssetRow.producer_tool_run_id == tool_run_id)
            .order_by(AssetRow.created_at, AssetRow.asset_id)
        )
        try:
            rows = self._session.scalars(statement).all()
        except SQLAlchemyError as error:
            _raise_safe_persistence_error(error)
        return [_from_row(row) for row in rows]

    def add(self, asset: Asset) -> None:
        try:
            self._session.add(
                AssetRow(
                    **{
                        field: getattr(asset, field)
                        for field in Asset.__dataclass_fields__
                    }
                )
            )
            self._session.flush()
        except SQLAlchemyError as error:
            _raise_safe_persistence_error(error)

    def update(self, asset: Asset, *, expected_status: str) -> Asset | None:
        try:
            row = self._session.get(
                AssetRow,
                asset.asset_id,
                populate_existing=True,
                with_for_update=True,
            )
            if row is None or row.current_status != expected_status:
                return None
            immutable = (
                "task_id",
                "producer_tool_run_id",
                "actor_id",
                "operation_id",
                "asset_type",
                "source_type",
                "role",
                "object_key",
                "pending_since",
                "created_at",
            )
            if any(getattr(row, field) != getattr(asset, field) for field in immutable):
                return None
            allowed = {
                "PENDING": {"AVAILABLE", "FAILED", "ORPHANED"},
                "AVAILABLE": {"ORPHANED"},
                "ORPHANED": {"AVAILABLE", "FAILED"},
            }
            if asset.current_status not in allowed.get(row.current_status, set()):
                return None
            for field in (
                "current_status",
                "media_type",
                "width",
                "height",
                "bit_depth",
                "sha256",
                "size_bytes",
                "encoding_rule",
                "available_at",
                "failed_at",
                "orphaned_at",
                "error_code",
                "safe_error_message",
                "orphan_reason",
                "orphan_details",
            ):
                setattr(row, field, getattr(asset, field))
            self._session.flush()
            return _from_row(row)
        except SQLAlchemyError as error:
            _raise_safe_persistence_error(error)
