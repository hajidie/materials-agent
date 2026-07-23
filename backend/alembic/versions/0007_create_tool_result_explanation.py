"""Create ToolResult, ResultAssetLink, and NaturalLanguageExplanation.

Revision ID: 0007_tool_result_explanation
Revises: 0006_asset
"""

from typing import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "0007_tool_result_explanation"
down_revision: str | None = "0006_asset"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint(
        "ck_llm_call_provider_request_id_not_blank",
        "llm_call",
        type_="check",
    )
    op.create_check_constraint(
        "ck_llm_call_provider_request_id_not_blank",
        "llm_call",
        "provider_request_id IS NULL OR "
        "(length(provider_request_id) <= 256 AND "
        "provider_request_id ~ "
        "'^[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}$')",
    )
    op.create_table(
        "tool_result",
        sa.Column("result_id", sa.Text(), nullable=False),
        sa.Column("task_id", sa.Text(), nullable=False),
        sa.Column("tool_run_id", sa.Text(), nullable=False),
        sa.Column("actor_id", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("requested_outputs", postgresql.ARRAY(sa.Text()), nullable=False),
        sa.Column("completed_outputs", postgresql.ARRAY(sa.Text()), nullable=False),
        sa.Column("failed_outputs", postgresql.ARRAY(sa.Text()), nullable=False),
        sa.Column("data", postgresql.JSONB(), nullable=False),
        sa.Column("warnings", postgresql.JSONB(), nullable=False),
        sa.Column("provenance", postgresql.JSONB(), nullable=False),
        sa.Column("error", postgresql.JSONB(), nullable=True),
        sa.Column("tool_id", sa.Text(), nullable=False),
        sa.Column("tool_version", sa.Text(), nullable=False),
        sa.Column("schema_version", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "length(btrim(result_id)) > 0",
            name="ck_tool_result_id_not_blank",
        ),
        sa.CheckConstraint(
            "length(btrim(task_id)) > 0",
            name="ck_tool_result_task_id_not_blank",
        ),
        sa.CheckConstraint(
            "length(btrim(tool_run_id)) > 0",
            name="ck_tool_result_tool_run_id_not_blank",
        ),
        sa.CheckConstraint(
            "length(btrim(actor_id)) > 0",
            name="ck_tool_result_actor_id_not_blank",
        ),
        sa.CheckConstraint(
            "length(btrim(tool_id)) > 0",
            name="ck_tool_result_tool_id_not_blank",
        ),
        sa.CheckConstraint(
            "length(btrim(tool_version)) > 0",
            name="ck_tool_result_tool_version_not_blank",
        ),
        sa.CheckConstraint(
            "length(btrim(schema_version)) > 0",
            name="ck_tool_result_schema_version_not_blank",
        ),
        sa.CheckConstraint(
            "status IN ('SUCCEEDED', 'PARTIALLY_SUCCEEDED', 'FAILED')",
            name="ck_tool_result_status_allowed",
        ),
        sa.CheckConstraint(
            "cardinality(requested_outputs) > 0",
            name="ck_tool_result_requested_nonempty",
        ),
        sa.CheckConstraint(
            "completed_outputs <@ requested_outputs "
            "AND failed_outputs <@ requested_outputs "
            "AND NOT (completed_outputs && failed_outputs) "
            "AND requested_outputs <@ (completed_outputs || failed_outputs)",
            name="ck_tool_result_output_sets",
        ),
        sa.CheckConstraint(
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
        sa.CheckConstraint(
            "jsonb_typeof(data) = 'object'",
            name="ck_tool_result_data_object",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(warnings) = 'array'",
            name="ck_tool_result_warnings_array",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(provenance) = 'object'",
            name="ck_tool_result_provenance_object",
        ),
        sa.CheckConstraint(
            "error IS NULL OR jsonb_typeof(error) = 'object'",
            name="ck_tool_result_error_object",
        ),
        sa.ForeignKeyConstraint(
            ["task_id"],
            ["task.task_id"],
            name="fk_tool_result_task",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tool_run_id"],
            ["tool_run.tool_run_id"],
            name="fk_tool_result_tool_run",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["actor_id"],
            ["actor.actor_id"],
            name="fk_tool_result_actor",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("result_id", name="pk_tool_result"),
        sa.UniqueConstraint("tool_run_id", name="uq_tool_result_tool_run_id"),
    )
    op.create_index(
        "ix_tool_result_task_created",
        "tool_result",
        ["task_id", "created_at", "result_id"],
    )

    op.create_table(
        "result_asset_link",
        sa.Column("result_id", sa.Text(), nullable=False),
        sa.Column("asset_id", sa.Text(), nullable=False),
        sa.Column("artifact_order", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "length(btrim(result_id)) > 0",
            name="ck_result_asset_link_result_id_not_blank",
        ),
        sa.CheckConstraint(
            "length(btrim(asset_id)) > 0",
            name="ck_result_asset_link_asset_id_not_blank",
        ),
        sa.CheckConstraint(
            "artifact_order >= 0",
            name="ck_result_asset_link_order_nonnegative",
        ),
        sa.ForeignKeyConstraint(
            ["result_id"],
            ["tool_result.result_id"],
            name="fk_result_asset_link_result",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["asset_id"],
            ["asset.asset_id"],
            name="fk_result_asset_link_asset",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint(
            "result_id",
            "asset_id",
            name="pk_result_asset_link",
        ),
        sa.UniqueConstraint(
            "result_id",
            "artifact_order",
            name="uq_result_asset_link_result_order",
        ),
    )

    op.create_foreign_key(
        "fk_task_selected_result",
        "task",
        "tool_result",
        ["selected_result_id"],
        ["result_id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_llm_call_input_result",
        "llm_call",
        "tool_result",
        ["input_result_id"],
        ["result_id"],
        ondelete="RESTRICT",
    )

    op.create_table(
        "natural_language_explanation",
        sa.Column("explanation_id", sa.Text(), nullable=False),
        sa.Column("task_id", sa.Text(), nullable=False),
        sa.Column("result_id", sa.Text(), nullable=False),
        sa.Column("llm_call_id", sa.Text(), nullable=False),
        sa.Column("attempt_no", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("language", sa.String(length=32), nullable=False),
        sa.Column("text", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("error_code", sa.String(length=128), nullable=True),
        sa.Column("safe_error_message", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "length(btrim(explanation_id)) > 0",
            name="ck_explanation_id_not_blank",
        ),
        sa.CheckConstraint(
            "length(btrim(task_id)) > 0",
            name="ck_explanation_task_id_not_blank",
        ),
        sa.CheckConstraint(
            "length(btrim(result_id)) > 0",
            name="ck_explanation_result_id_not_blank",
        ),
        sa.CheckConstraint(
            "length(btrim(llm_call_id)) > 0",
            name="ck_explanation_llm_call_id_not_blank",
        ),
        sa.CheckConstraint(
            "attempt_no > 0",
            name="ck_explanation_attempt_positive",
        ),
        sa.CheckConstraint(
            "status IN ('PENDING', 'RUNNING', 'SUCCEEDED', 'FAILED')",
            name="ck_explanation_status_allowed",
        ),
        sa.CheckConstraint(
            "length(btrim(language)) > 0",
            name="ck_explanation_language_not_blank",
        ),
        sa.CheckConstraint(
            "error_code IS NULL OR "
            "(length(btrim(error_code)) > 0 AND length(error_code) <= 64)",
            name="ck_explanation_error_code_safe",
        ),
        sa.CheckConstraint(
            "safe_error_message IS NULL OR "
            "(length(btrim(safe_error_message)) > 0 "
            "AND length(safe_error_message) <= 256)",
            name="ck_explanation_safe_error_message_safe",
        ),
        sa.CheckConstraint(
            "duration_ms IS NULL OR duration_ms >= 0",
            name="ck_explanation_duration_nonnegative",
        ),
        sa.CheckConstraint(
            "started_at IS NULL OR started_at >= created_at",
            name="ck_explanation_started_not_before_created",
        ),
        sa.CheckConstraint(
            "completed_at IS NULL OR "
            "(started_at IS NOT NULL AND completed_at >= started_at)",
            name="ck_explanation_completed_not_before_started",
        ),
        sa.CheckConstraint(
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
        sa.ForeignKeyConstraint(
            ["task_id"],
            ["task.task_id"],
            name="fk_explanation_task",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["result_id"],
            ["tool_result.result_id"],
            name="fk_explanation_result",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["llm_call_id"],
            ["llm_call.llm_call_id"],
            name="fk_explanation_llm_call",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint(
            "explanation_id",
            name="pk_natural_language_explanation",
        ),
        sa.UniqueConstraint(
            "result_id",
            "attempt_no",
            name="uq_explanation_result_attempt",
        ),
        sa.UniqueConstraint(
            "llm_call_id",
            name="uq_explanation_llm_call_id",
        ),
    )


def downgrade() -> None:
    op.drop_table("natural_language_explanation")
    op.execute(
        sa.text(
            "DELETE FROM llm_call "
            "WHERE purpose = 'TOOL_RESULT_EXPLANATION'"
        )
    )
    op.execute(
        sa.text(
            "UPDATE task SET selected_result_id = NULL "
            "WHERE selected_result_id IN "
            "(SELECT result_id FROM tool_result)"
        )
    )
    remaining_input_results = op.get_bind().execute(
        sa.text(
            "SELECT count(*) FROM llm_call "
            "WHERE input_result_id IS NOT NULL"
        )
    ).scalar_one()
    if remaining_input_results:
        raise RuntimeError(
            "Cannot downgrade while LLMCall Result references remain."
        )
    op.drop_constraint(
        "fk_llm_call_input_result",
        "llm_call",
        type_="foreignkey",
    )
    op.drop_constraint(
        "fk_task_selected_result",
        "task",
        type_="foreignkey",
    )
    op.drop_table("result_asset_link")
    op.drop_index("ix_tool_result_task_created", table_name="tool_result")
    op.drop_table("tool_result")
    op.drop_constraint(
        "ck_llm_call_provider_request_id_not_blank",
        "llm_call",
        type_="check",
    )
    op.create_check_constraint(
        "ck_llm_call_provider_request_id_not_blank",
        "llm_call",
        "provider_request_id IS NULL OR "
        "length(btrim(provider_request_id)) > 0",
    )
