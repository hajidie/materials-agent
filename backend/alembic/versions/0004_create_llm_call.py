"""Create LLMCall and attach existing LLM source references.

Revision ID: 0004_llm_call
Revises: 0003_task_time_order
"""

from typing import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "0004_llm_call"
down_revision: str | None = "0003_task_time_order"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "llm_call",
        sa.Column("llm_call_id", sa.Text(), nullable=False),
        sa.Column("task_id", sa.Text(), nullable=False),
        sa.Column("conversation_id", sa.Text(), nullable=False),
        sa.Column("request_id", sa.Text(), nullable=False),
        sa.Column("purpose", sa.String(length=64), nullable=False),
        sa.Column("input_result_id", sa.Text(), nullable=True),
        sa.Column("provider", sa.String(length=128), nullable=False),
        sa.Column("model_name", sa.String(length=256), nullable=False),
        sa.Column("prompt_template_id", sa.Text(), nullable=True),
        sa.Column(
            "prompt_template_version",
            sa.String(length=64),
            nullable=True,
        ),
        sa.Column("prompt_digest", sa.String(length=64), nullable=True),
        sa.Column(
            "generation_parameters",
            postgresql.JSONB(),
            nullable=False,
        ),
        sa.Column(
            "structured_output_summary",
            postgresql.JSONB(),
            nullable=True,
        ),
        sa.Column("usage", postgresql.JSONB(), nullable=True),
        sa.Column("provider_request_id", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("error_code", sa.String(length=128), nullable=True),
        sa.Column("safe_error_message", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "length(btrim(llm_call_id)) > 0",
            name="ck_llm_call_llm_call_id_not_blank",
        ),
        sa.CheckConstraint(
            "length(btrim(task_id)) > 0",
            name="ck_llm_call_task_id_not_blank",
        ),
        sa.CheckConstraint(
            "length(btrim(conversation_id)) > 0",
            name="ck_llm_call_conversation_id_not_blank",
        ),
        sa.CheckConstraint(
            "length(btrim(request_id)) > 0",
            name="ck_llm_call_request_id_not_blank",
        ),
        sa.CheckConstraint(
            "length(btrim(provider)) > 0",
            name="ck_llm_call_provider_not_blank",
        ),
        sa.CheckConstraint(
            "length(btrim(model_name)) > 0",
            name="ck_llm_call_model_name_not_blank",
        ),
        sa.CheckConstraint(
            "input_result_id IS NULL OR length(btrim(input_result_id)) > 0",
            name="ck_llm_call_input_result_id_not_blank",
        ),
        sa.CheckConstraint(
            "prompt_template_id IS NULL OR "
            "length(btrim(prompt_template_id)) > 0",
            name="ck_llm_call_prompt_template_id_not_blank",
        ),
        sa.CheckConstraint(
            "prompt_template_version IS NULL OR "
            "length(btrim(prompt_template_version)) > 0",
            name="ck_llm_call_prompt_template_version_not_blank",
        ),
        sa.CheckConstraint(
            "prompt_digest IS NULL OR prompt_digest ~ '^[0-9a-f]{64}$'",
            name="ck_llm_call_prompt_digest_sha256",
        ),
        sa.CheckConstraint(
            "provider_request_id IS NULL OR "
            "length(btrim(provider_request_id)) > 0",
            name="ck_llm_call_provider_request_id_not_blank",
        ),
        sa.CheckConstraint(
            "error_code IS NULL OR length(btrim(error_code)) > 0",
            name="ck_llm_call_error_code_not_blank",
        ),
        sa.CheckConstraint(
            "safe_error_message IS NULL OR "
            "length(btrim(safe_error_message)) > 0",
            name="ck_llm_call_safe_error_message_not_blank",
        ),
        sa.CheckConstraint(
            "purpose IN ('CHAT_ORCHESTRATION', 'TOOL_RESULT_EXPLANATION')",
            name="ck_llm_call_purpose_allowed",
        ),
        sa.CheckConstraint(
            "(purpose = 'CHAT_ORCHESTRATION' AND input_result_id IS NULL) "
            "OR (purpose = 'TOOL_RESULT_EXPLANATION' "
            "AND input_result_id IS NOT NULL)",
            name="ck_llm_call_input_result_purpose",
        ),
        sa.CheckConstraint(
            "status IN ('PENDING', 'RUNNING', 'SUCCEEDED', 'FAILED')",
            name="ck_llm_call_status_allowed",
        ),
        sa.CheckConstraint(
            "duration_ms IS NULL OR duration_ms >= 0",
            name="ck_llm_call_duration_nonnegative",
        ),
        sa.CheckConstraint(
            "started_at IS NULL OR started_at >= created_at",
            name="ck_llm_call_started_not_before_created",
        ),
        sa.CheckConstraint(
            "completed_at IS NULL OR "
            "(started_at IS NOT NULL AND completed_at >= started_at)",
            name="ck_llm_call_completed_not_before_started",
        ),
        sa.CheckConstraint(
            "(status = 'PENDING' AND started_at IS NULL "
            "AND completed_at IS NULL AND duration_ms IS NULL) OR "
            "(status = 'RUNNING' AND started_at IS NOT NULL "
            "AND completed_at IS NULL AND duration_ms IS NULL) OR "
            "(status IN ('SUCCEEDED', 'FAILED') AND started_at IS NOT NULL "
            "AND completed_at IS NOT NULL AND duration_ms IS NOT NULL)",
            name="ck_llm_call_status_time_shape",
        ),
        sa.CheckConstraint(
            "status <> 'SUCCEEDED' OR "
            "(error_code IS NULL AND safe_error_message IS NULL)",
            name="ck_llm_call_succeeded_without_error",
        ),
        sa.CheckConstraint(
            "status <> 'FAILED' OR error_code IS NOT NULL",
            name="ck_llm_call_failed_requires_error",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(generation_parameters) = 'object'",
            name="ck_llm_call_generation_parameters_object",
        ),
        sa.CheckConstraint(
            "structured_output_summary IS NULL OR "
            "jsonb_typeof(structured_output_summary) = 'object'",
            name="ck_llm_call_structured_output_summary_object",
        ),
        sa.CheckConstraint(
            "usage IS NULL OR jsonb_typeof(usage) = 'object'",
            name="ck_llm_call_usage_object",
        ),
        sa.ForeignKeyConstraint(
            ["conversation_id"],
            ["conversation.conversation_id"],
            name="fk_llm_call_conversation",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["task_id"],
            ["task.task_id"],
            name="fk_llm_call_task",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("llm_call_id", name="pk_llm_call"),
    )
    op.create_index(
        "ix_llm_call_task_request_created",
        "llm_call",
        ["task_id", "request_id", "created_at"],
        unique=False,
    )
    op.create_foreign_key(
        "fk_message_llm_call",
        "message",
        "llm_call",
        ["llm_call_id"],
        ["llm_call_id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_task_input_revision_llm_call",
        "task_input_revision",
        "llm_call",
        ["source_llm_call_id"],
        ["llm_call_id"],
        ondelete="RESTRICT",
    )


def downgrade() -> None:
    op.drop_constraint(
        "fk_task_input_revision_llm_call",
        "task_input_revision",
        type_="foreignkey",
    )
    op.drop_constraint(
        "fk_message_llm_call",
        "message",
        type_="foreignkey",
    )
    op.drop_index(
        "ix_llm_call_task_request_created",
        table_name="llm_call",
    )
    op.drop_table("llm_call")
