"""Create ToolRun and attach Task selected ToolRun reference.

Revision ID: 0005_tool_run
Revises: 0004_llm_call
"""

from typing import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "0005_tool_run"
down_revision: str | None = "0004_llm_call"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "tool_run",
        sa.Column("tool_run_id", sa.Text(), nullable=False),
        sa.Column("task_id", sa.Text(), nullable=False),
        sa.Column("request_id", sa.Text(), nullable=False),
        sa.Column("task_input_revision_id", sa.Text(), nullable=False),
        sa.Column("attempt_no", sa.Integer(), nullable=False),
        sa.Column("tool_id", sa.Text(), nullable=False),
        sa.Column("tool_version", sa.String(length=64), nullable=False),
        sa.Column("schema_version", sa.String(length=64), nullable=False),
        sa.Column("requested_outputs", postgresql.ARRAY(sa.Text()), nullable=False),
        sa.Column("completed_outputs", postgresql.ARRAY(sa.Text()), nullable=False),
        sa.Column("failed_outputs", postgresql.ARRAY(sa.Text()), nullable=False),
        sa.Column("execution_input", postgresql.JSONB(), nullable=False),
        sa.Column("actual_runtime_parameters", postgresql.JSONB(), nullable=True),
        sa.Column("diagnostics", postgresql.JSONB(), nullable=False),
        sa.Column("output_summary", postgresql.JSONB(), nullable=True),
        sa.Column("current_status", sa.String(length=32), nullable=False),
        sa.Column("model_bundle_id", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("error_code", sa.String(length=128), nullable=True),
        sa.Column("safe_error_message", sa.Text(), nullable=True),
        sa.CheckConstraint("length(btrim(tool_run_id)) > 0", name="ck_tool_run_id_not_blank"),
        sa.CheckConstraint("length(btrim(task_id)) > 0", name="ck_tool_run_task_id_not_blank"),
        sa.CheckConstraint("length(btrim(request_id)) > 0", name="ck_tool_run_request_id_not_blank"),
        sa.CheckConstraint("length(btrim(task_input_revision_id)) > 0", name="ck_tool_run_revision_id_not_blank"),
        sa.CheckConstraint("attempt_no > 0", name="ck_tool_run_attempt_positive"),
        sa.CheckConstraint("length(btrim(tool_id)) > 0", name="ck_tool_run_tool_id_not_blank"),
        sa.CheckConstraint("length(btrim(tool_version)) > 0", name="ck_tool_run_tool_version_not_blank"),
        sa.CheckConstraint("length(btrim(schema_version)) > 0", name="ck_tool_run_schema_version_not_blank"),
        sa.CheckConstraint("cardinality(requested_outputs) > 0", name="ck_tool_run_requested_outputs_nonempty"),
        sa.CheckConstraint("completed_outputs <@ requested_outputs", name="ck_tool_run_completed_subset"),
        sa.CheckConstraint("failed_outputs <@ requested_outputs", name="ck_tool_run_failed_subset"),
        sa.CheckConstraint("NOT (completed_outputs && failed_outputs)", name="ck_tool_run_output_sets_disjoint"),
        sa.CheckConstraint("jsonb_typeof(execution_input) = 'object'", name="ck_tool_run_execution_input_object"),
        sa.CheckConstraint("actual_runtime_parameters IS NULL OR jsonb_typeof(actual_runtime_parameters) = 'object'", name="ck_tool_run_runtime_parameters_object"),
        sa.CheckConstraint("jsonb_typeof(diagnostics) = 'array'", name="ck_tool_run_diagnostics_array"),
        sa.CheckConstraint("output_summary IS NULL OR jsonb_typeof(output_summary) = 'object'", name="ck_tool_run_output_summary_object"),
        sa.CheckConstraint("current_status IN ('PENDING', 'RUNNING', 'SUCCEEDED', 'PARTIALLY_SUCCEEDED', 'FAILED')", name="ck_tool_run_status_allowed"),
        sa.CheckConstraint("model_bundle_id IS NULL OR length(btrim(model_bundle_id)) > 0", name="ck_tool_run_model_bundle_not_blank"),
        sa.CheckConstraint("started_at IS NULL OR started_at >= created_at", name="ck_tool_run_started_not_before_created"),
        sa.CheckConstraint("completed_at IS NULL OR (started_at IS NOT NULL AND completed_at >= started_at)", name="ck_tool_run_completed_not_before_started"),
        sa.CheckConstraint("duration_ms IS NULL OR duration_ms >= 0", name="ck_tool_run_duration_nonnegative"),
        sa.CheckConstraint("error_code IS NULL OR length(btrim(error_code)) > 0", name="ck_tool_run_error_code_not_blank"),
        sa.CheckConstraint("safe_error_message IS NULL OR length(btrim(safe_error_message)) > 0", name="ck_tool_run_safe_error_not_blank"),
        sa.CheckConstraint("(current_status = 'PENDING' AND started_at IS NULL AND completed_at IS NULL AND duration_ms IS NULL AND actual_runtime_parameters IS NULL AND output_summary IS NULL) OR (current_status = 'RUNNING' AND started_at IS NOT NULL AND completed_at IS NULL AND duration_ms IS NULL) OR (current_status IN ('SUCCEEDED', 'PARTIALLY_SUCCEEDED', 'FAILED') AND started_at IS NOT NULL AND completed_at IS NOT NULL AND duration_ms IS NOT NULL)", name="ck_tool_run_status_time_shape"),
        sa.CheckConstraint("current_status <> 'FAILED' OR (cardinality(completed_outputs) = 0 AND error_code IS NOT NULL)", name="ck_tool_run_failed_shape"),
        sa.ForeignKeyConstraint(["task_id"], ["task.task_id"], name="fk_tool_run_task", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["task_input_revision_id"], ["task_input_revision.task_input_revision_id"], name="fk_tool_run_revision", ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("tool_run_id", name="pk_tool_run"),
        sa.UniqueConstraint("task_id", "attempt_no", name="uq_tool_run_task_attempt"),
    )
    op.create_foreign_key(
        "fk_task_selected_tool_run",
        "task",
        "tool_run",
        ["selected_tool_run_id"],
        ["tool_run_id"],
        ondelete="RESTRICT",
        use_alter=True,
    )


def downgrade() -> None:
    op.drop_constraint("fk_task_selected_tool_run", "task", type_="foreignkey")
    op.drop_table("tool_run")
