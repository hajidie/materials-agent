"""Create generated Asset lifecycle anchors.

Revision ID: 0006_asset
Revises: 0005_tool_run
"""

from typing import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "0006_asset"
down_revision: str | None = "0005_tool_run"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "asset",
        sa.Column("asset_id", sa.Text(), nullable=False),
        sa.Column("task_id", sa.Text(), nullable=False),
        sa.Column("producer_tool_run_id", sa.Text(), nullable=False),
        sa.Column("actor_id", sa.Text(), nullable=False),
        sa.Column("operation_id", sa.Text(), nullable=False),
        sa.Column("current_status", sa.String(length=32), nullable=False),
        sa.Column("asset_type", sa.String(length=64), nullable=False),
        sa.Column("source_type", sa.String(length=32), nullable=False),
        sa.Column("role", sa.String(length=64), nullable=False),
        sa.Column("object_key", sa.Text(), nullable=False),
        sa.Column("media_type", sa.String(length=128), nullable=True),
        sa.Column("width", sa.Integer(), nullable=True),
        sa.Column("height", sa.Integer(), nullable=True),
        sa.Column("bit_depth", sa.Integer(), nullable=True),
        sa.Column("sha256", sa.String(length=64), nullable=True),
        sa.Column("size_bytes", sa.Integer(), nullable=True),
        sa.Column("encoding_rule", sa.Text(), nullable=True),
        sa.Column("pending_since", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("failed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("orphaned_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_code", sa.String(length=128), nullable=True),
        sa.Column("safe_error_message", sa.Text(), nullable=True),
        sa.Column("orphan_reason", sa.String(length=128), nullable=True),
        sa.Column("orphan_details", postgresql.JSONB(), nullable=True),
        sa.CheckConstraint("length(btrim(asset_id)) > 0", name="ck_asset_id_not_blank"),
        sa.CheckConstraint("length(btrim(task_id)) > 0", name="ck_asset_task_id_not_blank"),
        sa.CheckConstraint("length(btrim(producer_tool_run_id)) > 0", name="ck_asset_tool_run_id_not_blank"),
        sa.CheckConstraint("length(btrim(actor_id)) > 0", name="ck_asset_actor_id_not_blank"),
        sa.CheckConstraint("length(btrim(operation_id)) > 0", name="ck_asset_operation_id_not_blank"),
        sa.CheckConstraint("current_status IN ('PENDING', 'AVAILABLE', 'FAILED', 'ORPHANED')", name="ck_asset_status_allowed"),
        sa.CheckConstraint("asset_type = 'sem_image'", name="ck_asset_type_sem_image"),
        sa.CheckConstraint("source_type = 'GENERATED' AND producer_tool_run_id IS NOT NULL", name="ck_asset_generated_source"),
        sa.CheckConstraint("role IN ('requested_output', 'intermediate', 'supporting')", name="ck_asset_role_allowed"),
        sa.CheckConstraint("length(btrim(object_key)) > 0", name="ck_asset_object_key_not_blank"),
        sa.CheckConstraint("media_type IS NULL OR length(btrim(media_type)) > 0", name="ck_asset_media_type_not_blank"),
        sa.CheckConstraint("width IS NULL OR width > 0", name="ck_asset_width_positive"),
        sa.CheckConstraint("height IS NULL OR height > 0", name="ck_asset_height_positive"),
        sa.CheckConstraint("bit_depth IS NULL OR bit_depth > 0", name="ck_asset_bit_depth_positive"),
        sa.CheckConstraint("sha256 IS NULL OR sha256 ~ '^[0-9a-f]{64}$'", name="ck_asset_sha256_format"),
        sa.CheckConstraint("size_bytes IS NULL OR size_bytes >= 0", name="ck_asset_size_nonnegative"),
        sa.CheckConstraint("encoding_rule IS NULL OR length(btrim(encoding_rule)) > 0", name="ck_asset_encoding_rule_not_blank"),
        sa.CheckConstraint("pending_since >= created_at", name="ck_asset_pending_not_before_created"),
        sa.CheckConstraint("available_at IS NULL OR available_at >= created_at", name="ck_asset_available_not_before_created"),
        sa.CheckConstraint("failed_at IS NULL OR failed_at >= created_at", name="ck_asset_failed_not_before_created"),
        sa.CheckConstraint("orphaned_at IS NULL OR orphaned_at >= created_at", name="ck_asset_orphaned_not_before_created"),
        sa.CheckConstraint("error_code IS NULL OR length(btrim(error_code)) > 0", name="ck_asset_error_code_not_blank"),
        sa.CheckConstraint("safe_error_message IS NULL OR length(btrim(safe_error_message)) > 0", name="ck_asset_safe_error_not_blank"),
        sa.CheckConstraint("orphan_reason IS NULL OR length(btrim(orphan_reason)) > 0", name="ck_asset_orphan_reason_not_blank"),
        sa.CheckConstraint("orphan_details IS NULL OR jsonb_typeof(orphan_details) = 'object'", name="ck_asset_orphan_details_object"),
        sa.CheckConstraint(
            "(current_status = 'PENDING' AND media_type IS NULL AND width IS NULL AND height IS NULL AND bit_depth IS NULL AND sha256 IS NULL AND size_bytes IS NULL AND encoding_rule IS NULL AND available_at IS NULL AND failed_at IS NULL AND orphaned_at IS NULL AND error_code IS NULL AND safe_error_message IS NULL AND orphan_reason IS NULL AND orphan_details IS NULL) OR "
            "(current_status = 'AVAILABLE' AND media_type IS NOT NULL AND width IS NOT NULL AND height IS NOT NULL AND bit_depth IS NOT NULL AND sha256 IS NOT NULL AND size_bytes IS NOT NULL AND encoding_rule IS NOT NULL AND available_at IS NOT NULL AND failed_at IS NULL AND orphaned_at IS NULL AND error_code IS NULL AND safe_error_message IS NULL AND orphan_reason IS NULL AND orphan_details IS NULL) OR "
            "(current_status = 'FAILED' AND media_type IS NULL AND width IS NULL AND height IS NULL AND bit_depth IS NULL AND sha256 IS NULL AND size_bytes IS NULL AND encoding_rule IS NULL AND available_at IS NULL AND failed_at IS NOT NULL AND orphaned_at IS NULL AND error_code IS NOT NULL AND safe_error_message IS NOT NULL AND orphan_reason IS NULL AND orphan_details IS NULL) OR "
            "(current_status = 'ORPHANED' AND failed_at IS NULL AND orphaned_at IS NOT NULL AND error_code IS NULL AND safe_error_message IS NULL AND orphan_reason IS NOT NULL AND orphan_details IS NOT NULL AND ((media_type IS NULL AND width IS NULL AND height IS NULL AND bit_depth IS NULL AND sha256 IS NULL AND size_bytes IS NULL AND encoding_rule IS NULL AND available_at IS NULL) OR (media_type IS NOT NULL AND width IS NOT NULL AND height IS NOT NULL AND bit_depth IS NOT NULL AND sha256 IS NOT NULL AND size_bytes IS NOT NULL AND encoding_rule IS NOT NULL AND available_at IS NOT NULL)))",
            name="ck_asset_status_shape",
        ),
        sa.ForeignKeyConstraint(["task_id"], ["task.task_id"], name="fk_asset_task", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["producer_tool_run_id"], ["tool_run.tool_run_id"], name="fk_asset_tool_run", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["actor_id"], ["actor.actor_id"], name="fk_asset_actor", ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("asset_id", name="pk_asset"),
        sa.UniqueConstraint("object_key", name="uq_asset_object_key"),
        sa.UniqueConstraint("operation_id", name="uq_asset_operation_id"),
    )
    op.create_index(
        "ix_asset_tool_run_created",
        "asset",
        ["producer_tool_run_id", "created_at", "asset_id"],
    )
    op.create_index(
        "ix_asset_status_pending",
        "asset",
        ["current_status", "pending_since"],
    )


def downgrade() -> None:
    op.drop_index("ix_asset_status_pending", table_name="asset")
    op.drop_index("ix_asset_tool_run_created", table_name="asset")
    op.drop_table("asset")
