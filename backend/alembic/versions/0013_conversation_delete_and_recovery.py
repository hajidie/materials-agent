"""Add idempotent Conversation create and durable object cleanup.

Revision ID: 0013_conversation_delete
Revises: 0012_llm_context_snapshot
"""

from __future__ import annotations

from typing import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "0013_conversation_delete"
down_revision: str | None = "0012_llm_context_snapshot"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_CASCADE_FOREIGN_KEYS = (
    ("task", "fk_task_conversation", "conversation_id", "conversation", "conversation_id", "CASCADE"),
    ("message", "fk_message_conversation", "conversation_id", "conversation", "conversation_id", "CASCADE"),
    ("message", "fk_message_task", "task_id", "task", "task_id", "CASCADE"),
    ("task_input_revision", "fk_task_input_revision_task", "task_id", "task", "task_id", "CASCADE"),
    ("task_input_revision", "fk_task_input_revision_llm_call", "source_llm_call_id", "llm_call", "llm_call_id", "SET NULL"),
    ("llm_call", "fk_llm_call_task", "task_id", "task", "task_id", "CASCADE"),
    ("llm_call", "fk_llm_call_conversation", "conversation_id", "conversation", "conversation_id", "CASCADE"),
    ("llm_call", "fk_llm_call_input_result", "input_result_id", "tool_result", "result_id", "SET NULL"),
    ("tool_run", "fk_tool_run_task", "task_id", "task", "task_id", "CASCADE"),
    ("tool_run", "fk_tool_run_revision", "task_input_revision_id", "task_input_revision", "task_input_revision_id", "CASCADE"),
    ("asset", "fk_asset_task", "task_id", "task", "task_id", "CASCADE"),
    ("asset", "fk_asset_tool_run", "producer_tool_run_id", "tool_run", "tool_run_id", "CASCADE"),
    ("tool_result", "fk_tool_result_task", "task_id", "task", "task_id", "CASCADE"),
    ("tool_result", "fk_tool_result_tool_run", "tool_run_id", "tool_run", "tool_run_id", "CASCADE"),
    ("result_asset_link", "fk_result_asset_link_result", "result_id", "tool_result", "result_id", "CASCADE"),
    ("result_asset_link", "fk_result_asset_link_asset", "asset_id", "asset", "asset_id", "CASCADE"),
    ("natural_language_explanation", "fk_explanation_task", "task_id", "task", "task_id", "CASCADE"),
    ("natural_language_explanation", "fk_explanation_result", "result_id", "tool_result", "result_id", "CASCADE"),
    ("natural_language_explanation", "fk_explanation_llm_call", "llm_call_id", "llm_call", "llm_call_id", "CASCADE"),
    ("idempotency_record", "fk_idempotency_task", "task_id", "task", "task_id", "CASCADE"),
    ("idempotency_record", "fk_idempotency_message", "message_id", "message", "message_id", "CASCADE"),
    ("idempotency_record", "fk_idempotency_revision", "task_input_revision_id", "task_input_revision", "task_input_revision_id", "CASCADE"),
    ("idempotency_record", "fk_idempotency_tool_run", "tool_run_id", "tool_run", "tool_run_id", "CASCADE"),
    ("idempotency_record", "fk_idempotency_explanation", "explanation_id", "natural_language_explanation", "explanation_id", "CASCADE"),
    ("task", "fk_task_selected_tool_run", "selected_tool_run_id", "tool_run", "tool_run_id", "SET NULL"),
    ("task", "fk_task_selected_result", "selected_result_id", "tool_result", "result_id", "SET NULL"),
)


def _replace_foreign_keys(rows: tuple[tuple[str, str, str, str, str, str], ...], *, downgrade: bool) -> None:
    for table, name, local, remote_table, remote, ondelete in rows:
        op.drop_constraint(name, table, type_="foreignkey")
        op.create_foreign_key(
            name,
            table,
            remote_table,
            [local],
            [remote],
            ondelete="RESTRICT" if downgrade else ondelete,
        )


def upgrade() -> None:
    op.add_column(
        "idempotency_record",
        sa.Column("conversation_id", sa.Text(), nullable=True),
    )
    op.execute(
        """
        UPDATE idempotency_record AS i
        SET conversation_id = t.conversation_id
        FROM task AS t
        WHERE i.task_id = t.task_id
          AND i.operation IN ('TASK_CREATE', 'TASK_INPUT_SUPPLEMENT')
        """
    )
    op.create_foreign_key(
        "fk_idempotency_conversation",
        "idempotency_record",
        "conversation",
        ["conversation_id"],
        ["conversation_id"],
        ondelete="CASCADE",
    )
    op.drop_constraint("ck_idempotency_operation_allowed", "idempotency_record", type_="check")
    op.drop_constraint("ck_idempotency_operation_binding", "idempotency_record", type_="check")
    op.create_check_constraint(
        "ck_idempotency_operation_allowed",
        "idempotency_record",
        "operation IN ('CONVERSATION_CREATE', 'TASK_CREATE', 'TASK_INPUT_SUPPLEMENT', 'TOOL_RETRY', 'EXPLANATION_RETRY')",
    )
    op.create_check_constraint(
        "ck_idempotency_operation_binding",
        "idempotency_record",
        "(operation = 'CONVERSATION_CREATE' AND conversation_id IS NOT NULL AND task_id IS NULL AND message_id IS NULL AND task_input_revision_id IS NULL AND tool_run_id IS NULL AND explanation_id IS NULL) OR "
        "(operation = 'TASK_CREATE' AND conversation_id IS NOT NULL AND task_id IS NOT NULL AND message_id IS NOT NULL AND task_input_revision_id IS NULL AND tool_run_id IS NULL AND explanation_id IS NULL) OR "
        "(operation = 'TASK_INPUT_SUPPLEMENT' AND conversation_id IS NOT NULL AND task_id IS NOT NULL AND message_id IS NOT NULL AND tool_run_id IS NULL AND explanation_id IS NULL) OR "
        "(operation = 'TOOL_RETRY' AND conversation_id IS NULL AND task_id IS NOT NULL AND message_id IS NULL AND task_input_revision_id IS NULL AND tool_run_id IS NOT NULL AND explanation_id IS NULL) OR "
        "(operation = 'EXPLANATION_RETRY' AND conversation_id IS NULL AND task_id IS NOT NULL AND message_id IS NULL AND task_input_revision_id IS NULL AND tool_run_id IS NULL AND explanation_id IS NOT NULL)",
    )

    op.add_column(
        "asset",
        sa.Column(
            "storage_identity_version",
            sa.String(length=32),
            nullable=False,
            server_default="LEGACY_DB_KEY",
        ),
    )
    op.create_check_constraint(
        "ck_asset_storage_identity_version_allowed",
        "asset",
        "storage_identity_version IN ('LEGACY_DB_KEY', 'METADATA_V1')",
    )
    op.add_column("asset", sa.Column("storage_bucket", sa.Text(), nullable=True))
    op.add_column("asset", sa.Column("storage_namespace", sa.Text(), nullable=True))
    op.create_check_constraint(
        "ck_asset_storage_bucket_not_blank",
        "asset",
        "storage_bucket IS NULL OR length(btrim(storage_bucket)) > 0",
    )
    op.create_check_constraint(
        "ck_asset_storage_namespace_not_blank",
        "asset",
        "storage_namespace IS NULL OR length(btrim(storage_namespace)) > 0",
    )
    op.create_check_constraint(
        "ck_asset_metadata_v1_storage_identity",
        "asset",
        "storage_identity_version = 'LEGACY_DB_KEY' OR "
        "(storage_bucket IS NOT NULL AND storage_namespace IS NOT NULL)",
    )
    op.alter_column("asset", "storage_identity_version", server_default=None)

    op.create_table(
        "conversation_object_cleanup",
        sa.Column("cleanup_id", sa.Text(), nullable=False),
        sa.Column("actor_id", sa.Text(), nullable=False),
        sa.Column("conversation_id", sa.Text(), nullable=False),
        sa.Column("asset_id", sa.Text(), nullable=False),
        sa.Column("operation_id", sa.Text(), nullable=False),
        sa.Column("producer_tool_run_id", sa.Text(), nullable=False),
        sa.Column("object_key", sa.Text(), nullable=False),
        sa.Column("bucket", sa.Text(), nullable=False),
        sa.Column("storage_namespace", sa.Text(), nullable=False),
        sa.Column("identity_version", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("safety_error_code", sa.String(length=128), nullable=True),
        sa.CheckConstraint("length(btrim(cleanup_id)) > 0", name="ck_cleanup_id_not_blank"),
        sa.CheckConstraint("length(btrim(actor_id)) > 0", name="ck_cleanup_actor_id_not_blank"),
        sa.CheckConstraint("length(btrim(conversation_id)) > 0", name="ck_cleanup_conversation_id_not_blank"),
        sa.CheckConstraint("length(btrim(asset_id)) > 0", name="ck_cleanup_asset_id_not_blank"),
        sa.CheckConstraint("length(btrim(operation_id)) > 0", name="ck_cleanup_operation_id_not_blank"),
        sa.CheckConstraint("length(btrim(producer_tool_run_id)) > 0", name="ck_cleanup_producer_run_id_not_blank"),
        sa.CheckConstraint("length(btrim(object_key)) > 0", name="ck_cleanup_object_key_not_blank"),
        sa.CheckConstraint("length(btrim(bucket)) > 0", name="ck_cleanup_bucket_not_blank"),
        sa.CheckConstraint("length(btrim(storage_namespace)) > 0", name="ck_cleanup_namespace_not_blank"),
        sa.CheckConstraint("identity_version IN ('LEGACY_DB_KEY', 'METADATA_V1')", name="ck_cleanup_identity_version_allowed"),
        sa.CheckConstraint("status IN ('PENDING', 'COMPLETED', 'SAFETY_BLOCKED')", name="ck_cleanup_status_allowed"),
        sa.CheckConstraint("attempts >= 0", name="ck_cleanup_attempts_nonnegative"),
        sa.CheckConstraint("safety_error_code IS NULL OR length(btrim(safety_error_code)) > 0", name="ck_cleanup_error_not_blank"),
        sa.PrimaryKeyConstraint("cleanup_id"),
        sa.UniqueConstraint("asset_id", name="uq_cleanup_asset"),
    )
    op.create_index(
        "ix_cleanup_pending_created",
        "conversation_object_cleanup",
        ["status", "created_at", "cleanup_id"],
    )
    _replace_foreign_keys(_CASCADE_FOREIGN_KEYS, downgrade=False)


def downgrade() -> None:
    _replace_foreign_keys(tuple(reversed(_CASCADE_FOREIGN_KEYS)), downgrade=True)
    op.drop_index("ix_cleanup_pending_created", table_name="conversation_object_cleanup")
    op.drop_table("conversation_object_cleanup")
    op.drop_constraint("ck_asset_metadata_v1_storage_identity", "asset", type_="check")
    op.drop_constraint("ck_asset_storage_namespace_not_blank", "asset", type_="check")
    op.drop_constraint("ck_asset_storage_bucket_not_blank", "asset", type_="check")
    op.drop_column("asset", "storage_namespace")
    op.drop_column("asset", "storage_bucket")
    op.drop_constraint("ck_asset_storage_identity_version_allowed", "asset", type_="check")
    op.drop_column("asset", "storage_identity_version")
    op.drop_constraint("ck_idempotency_operation_binding", "idempotency_record", type_="check")
    op.drop_constraint("ck_idempotency_operation_allowed", "idempotency_record", type_="check")
    op.create_check_constraint(
        "ck_idempotency_operation_allowed",
        "idempotency_record",
        "operation IN ('TASK_CREATE', 'TASK_INPUT_SUPPLEMENT', 'TOOL_RETRY', 'EXPLANATION_RETRY')",
    )
    op.create_check_constraint(
        "ck_idempotency_operation_binding",
        "idempotency_record",
        "(operation = 'TASK_CREATE' AND task_id IS NOT NULL AND message_id IS NOT NULL AND task_input_revision_id IS NULL AND tool_run_id IS NULL AND explanation_id IS NULL) OR "
        "(operation = 'TASK_INPUT_SUPPLEMENT' AND task_id IS NOT NULL AND message_id IS NOT NULL AND tool_run_id IS NULL AND explanation_id IS NULL) OR "
        "(operation = 'TOOL_RETRY' AND task_id IS NOT NULL AND message_id IS NULL AND task_input_revision_id IS NULL AND tool_run_id IS NOT NULL AND explanation_id IS NULL) OR "
        "(operation = 'EXPLANATION_RETRY' AND task_id IS NOT NULL AND message_id IS NULL AND task_input_revision_id IS NULL AND tool_run_id IS NULL AND explanation_id IS NOT NULL)",
    )
    op.drop_constraint("fk_idempotency_conversation", "idempotency_record", type_="foreignkey")
    op.drop_column("idempotency_record", "conversation_id")
