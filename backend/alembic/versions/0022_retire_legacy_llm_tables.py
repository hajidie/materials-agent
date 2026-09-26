"""Retire the pre-Agent Runtime LLM call and explanation schema."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB


revision = "0022_retire_legacy_llm_tables"
down_revision = "0021_resource_ref_protocol"
branch_labels = depends_on = None


def upgrade():
    op.drop_constraint("ck_idempotency_operation_binding", "idempotency_record", type_="check")
    op.drop_constraint("ck_idempotency_operation_allowed", "idempotency_record", type_="check")
    op.drop_constraint("fk_idempotency_explanation", "idempotency_record", type_="foreignkey")
    op.execute("DELETE FROM idempotency_record WHERE operation = 'EXPLANATION_RETRY'")
    op.drop_column("idempotency_record", "explanation_id")
    op.create_check_constraint(
        "ck_idempotency_operation_allowed",
        "idempotency_record",
        "operation IN ('CONVERSATION_CREATE', 'MESSAGE_SUBMIT', 'TASK_CREATE', "
        "'TASK_INPUT_SUPPLEMENT', 'TOOL_RETRY')",
    )
    op.create_check_constraint(
        "ck_idempotency_operation_binding",
        "idempotency_record",
        "(operation = 'CONVERSATION_CREATE' AND conversation_id IS NOT NULL "
        "AND task_id IS NULL AND message_id IS NULL AND task_input_revision_id IS NULL "
        "AND tool_run_id IS NULL) OR "
        "(operation = 'MESSAGE_SUBMIT' AND conversation_id IS NOT NULL "
        "AND task_id IS NULL AND message_id IS NOT NULL AND task_input_revision_id IS NULL "
        "AND tool_run_id IS NULL) OR "
        "(operation = 'TASK_CREATE' AND conversation_id IS NOT NULL "
        "AND task_id IS NOT NULL AND message_id IS NOT NULL "
        "AND task_input_revision_id IS NULL AND tool_run_id IS NULL) OR "
        "(operation = 'TASK_INPUT_SUPPLEMENT' AND conversation_id IS NOT NULL "
        "AND task_id IS NOT NULL AND message_id IS NOT NULL AND tool_run_id IS NULL) OR "
        "(operation = 'TOOL_RETRY' AND conversation_id IS NULL "
        "AND task_id IS NOT NULL AND message_id IS NULL "
        "AND task_input_revision_id IS NULL AND tool_run_id IS NOT NULL)",
    )

    op.drop_constraint("fk_task_input_revision_llm_call", "task_input_revision", type_="foreignkey")
    op.drop_constraint(
        "ck_task_input_revision_source_llm_call_id_not_blank",
        "task_input_revision",
        type_="check",
    )
    op.drop_column("task_input_revision", "source_llm_call_id")

    op.drop_constraint("fk_message_llm_call", "message", type_="foreignkey")
    op.drop_constraint("uq_message_llm_call_id", "message", type_="unique")
    op.drop_constraint("ck_message_llm_source_role", "message", type_="check")
    op.drop_constraint("ck_message_user_role_source", "message", type_="check")
    op.drop_constraint("ck_message_llm_call_id_not_blank", "message", type_="check")
    op.drop_constraint("ck_message_generation_source_allowed", "message", type_="check")
    op.drop_column("message", "llm_call_id")
    op.execute("UPDATE message SET generation_source = 'AGENT' WHERE generation_source = 'LLM'")
    op.create_check_constraint(
        "ck_message_generation_source_allowed",
        "message",
        "generation_source IN ('USER', 'TEMPLATE', 'AGENT')",
    )
    op.create_check_constraint(
        "ck_message_user_role_source",
        "message",
        "role <> 'USER' OR generation_source = 'USER'",
    )

    op.drop_table("natural_language_explanation")
    op.drop_table("llm_call")


def downgrade():
    op.create_table(
        "llm_call",
        sa.Column("llm_call_id", sa.Text(), primary_key=True),
        sa.Column("task_id", sa.Text(), nullable=True),
        sa.Column("conversation_id", sa.Text(), nullable=False),
        sa.Column("source_message_id", sa.Text(), nullable=False),
        sa.Column("request_id", sa.Text(), nullable=False),
        sa.Column("purpose", sa.String(64), nullable=False),
        sa.Column("input_result_id", sa.Text(), nullable=True),
        sa.Column("provider", sa.String(128), nullable=False),
        sa.Column("model_name", sa.String(256), nullable=False),
        sa.Column("prompt_template_id", sa.Text(), nullable=True),
        sa.Column("prompt_template_version", sa.String(64), nullable=True),
        sa.Column("prompt_digest", sa.String(64), nullable=True),
        sa.Column("catalog_snapshot_refs", JSONB(none_as_null=True), nullable=True),
        sa.Column("catalog_hash", sa.String(64), nullable=True),
        sa.Column("tool_context_ref", JSONB(none_as_null=True), nullable=True),
        sa.Column("generation_parameters", JSONB(), nullable=False),
        sa.Column("structured_output_summary", JSONB(none_as_null=True), nullable=True),
        sa.Column("usage", JSONB(none_as_null=True), nullable=True),
        sa.Column("context_snapshot", JSONB(none_as_null=True), nullable=True),
        sa.Column("provider_request_id", sa.Text(), nullable=True),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("error_code", sa.String(128), nullable=True),
        sa.Column("safe_error_message", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["task_id"], ["task.task_id"], name="fk_llm_call_task", ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["conversation_id"], ["conversation.conversation_id"],
            name="fk_llm_call_conversation", ondelete="CASCADE",
        ),
        sa.CheckConstraint("length(btrim(llm_call_id)) > 0", name="ck_llm_call_llm_call_id_not_blank"),
        sa.CheckConstraint("task_id IS NULL OR length(btrim(task_id)) > 0", name="ck_llm_call_task_id_not_blank"),
        sa.CheckConstraint("length(btrim(source_message_id)) > 0", name="ck_llm_call_source_message_not_blank"),
        sa.CheckConstraint("length(btrim(conversation_id)) > 0", name="ck_llm_call_conversation_id_not_blank"),
        sa.CheckConstraint("length(btrim(request_id)) > 0", name="ck_llm_call_request_id_not_blank"),
        sa.CheckConstraint("length(btrim(provider)) > 0", name="ck_llm_call_provider_not_blank"),
        sa.CheckConstraint("length(btrim(model_name)) > 0", name="ck_llm_call_model_name_not_blank"),
        sa.CheckConstraint("input_result_id IS NULL OR length(btrim(input_result_id)) > 0", name="ck_llm_call_input_result_id_not_blank"),
        sa.CheckConstraint("prompt_template_id IS NULL OR length(btrim(prompt_template_id)) > 0", name="ck_llm_call_prompt_template_id_not_blank"),
        sa.CheckConstraint("prompt_template_version IS NULL OR length(btrim(prompt_template_version)) > 0", name="ck_llm_call_prompt_template_version_not_blank"),
        sa.CheckConstraint("prompt_digest IS NULL OR prompt_digest ~ '^[0-9a-f]{64}$'", name="ck_llm_call_prompt_digest_sha256"),
        sa.CheckConstraint(
            "provider_request_id IS NULL OR (length(provider_request_id) <= 256 "
            "AND provider_request_id ~ '^[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}$')",
            name="ck_llm_call_provider_request_id_not_blank",
        ),
        sa.CheckConstraint("error_code IS NULL OR length(btrim(error_code)) > 0", name="ck_llm_call_error_code_not_blank"),
        sa.CheckConstraint("safe_error_message IS NULL OR length(btrim(safe_error_message)) > 0", name="ck_llm_call_safe_error_message_not_blank"),
        sa.CheckConstraint(
            "purpose IN ('CHAT_ORCHESTRATION', 'TOOL_RESULT_EXPLANATION', 'TOOL_INPUT_EXTRACTION')",
            name="ck_llm_call_purpose_allowed",
        ),
        sa.CheckConstraint(
            "(purpose = 'CHAT_ORCHESTRATION' AND input_result_id IS NULL) OR "
            "(purpose = 'TOOL_RESULT_EXPLANATION' AND input_result_id IS NOT NULL) OR "
            "(purpose = 'TOOL_INPUT_EXTRACTION' AND input_result_id IS NULL)",
            name="ck_llm_call_input_result_purpose",
        ),
        sa.CheckConstraint("catalog_snapshot_refs IS NULL OR jsonb_typeof(catalog_snapshot_refs) = 'array'", name="ck_llm_call_catalog_snapshot_refs_array"),
        sa.CheckConstraint("catalog_hash IS NULL OR catalog_hash ~ '^[0-9a-f]{64}$'", name="ck_llm_call_catalog_hash_sha256"),
        sa.CheckConstraint("tool_context_ref IS NULL OR jsonb_typeof(tool_context_ref) = 'object'", name="ck_llm_call_tool_context_ref_object"),
        sa.CheckConstraint("status IN ('PENDING', 'RUNNING', 'SUCCEEDED', 'FAILED')", name="ck_llm_call_status_allowed"),
        sa.CheckConstraint("duration_ms IS NULL OR duration_ms >= 0", name="ck_llm_call_duration_nonnegative"),
        sa.CheckConstraint("started_at IS NULL OR started_at >= created_at", name="ck_llm_call_started_not_before_created"),
        sa.CheckConstraint("completed_at IS NULL OR (started_at IS NOT NULL AND completed_at >= started_at)", name="ck_llm_call_completed_not_before_started"),
        sa.CheckConstraint(
            "(status = 'PENDING' AND started_at IS NULL AND completed_at IS NULL AND duration_ms IS NULL) OR "
            "(status = 'RUNNING' AND started_at IS NOT NULL AND completed_at IS NULL AND duration_ms IS NULL) OR "
            "(status IN ('SUCCEEDED', 'FAILED') AND started_at IS NOT NULL "
            "AND completed_at IS NOT NULL AND duration_ms IS NOT NULL)",
            name="ck_llm_call_status_time_shape",
        ),
        sa.CheckConstraint("status <> 'SUCCEEDED' OR (error_code IS NULL AND safe_error_message IS NULL)", name="ck_llm_call_succeeded_without_error"),
        sa.CheckConstraint("status <> 'FAILED' OR error_code IS NOT NULL", name="ck_llm_call_failed_requires_error"),
        sa.CheckConstraint("jsonb_typeof(generation_parameters) = 'object'", name="ck_llm_call_generation_parameters_object"),
        sa.CheckConstraint("structured_output_summary IS NULL OR jsonb_typeof(structured_output_summary) = 'object'", name="ck_llm_call_structured_output_summary_object"),
        sa.CheckConstraint("usage IS NULL OR jsonb_typeof(usage) = 'object'", name="ck_llm_call_usage_object"),
        sa.CheckConstraint("context_snapshot IS NULL OR jsonb_typeof(context_snapshot) = 'object'", name="ck_llm_call_context_snapshot_object"),
    )
    op.create_foreign_key(
        "fk_llm_call_input_result", "llm_call", "tool_result",
        ["input_result_id"], ["result_id"], ondelete="SET NULL",
    )
    op.create_foreign_key(
        "fk_llm_call_source_message_conversation", "llm_call", "message",
        ["source_message_id", "conversation_id"], ["message_id", "conversation_id"],
        ondelete="CASCADE",
    )
    op.create_index("ix_llm_call_task_request_created", "llm_call", ["task_id", "request_id", "created_at"])
    op.create_index(
        "ix_llm_call_source_message_created",
        "llm_call",
        ["source_message_id", "created_at", "llm_call_id"],
    )

    op.add_column("message", sa.Column("llm_call_id", sa.Text(), nullable=True))
    op.create_foreign_key(
        "fk_message_llm_call", "message", "llm_call", ["llm_call_id"], ["llm_call_id"], ondelete="RESTRICT"
    )
    op.create_unique_constraint("uq_message_llm_call_id", "message", ["llm_call_id"])
    op.drop_constraint("ck_message_generation_source_allowed", "message", type_="check")
    op.drop_constraint("ck_message_user_role_source", "message", type_="check")
    op.create_check_constraint(
        "ck_message_generation_source_allowed", "message",
        "generation_source IN ('USER', 'LLM', 'TEMPLATE', 'AGENT')",
    )
    op.create_check_constraint(
        "ck_message_llm_call_id_not_blank", "message",
        "llm_call_id IS NULL OR length(btrim(llm_call_id)) > 0",
    )
    op.create_check_constraint(
        "ck_message_user_role_source", "message",
        "role <> 'USER' OR (generation_source = 'USER' AND llm_call_id IS NULL)",
    )
    op.create_check_constraint(
        "ck_message_llm_source_role", "message",
        "generation_source <> 'LLM' OR (role = 'ASSISTANT' AND llm_call_id IS NOT NULL)",
    )

    op.add_column("task_input_revision", sa.Column("source_llm_call_id", sa.Text(), nullable=True))
    op.create_foreign_key(
        "fk_task_input_revision_llm_call", "task_input_revision", "llm_call",
        ["source_llm_call_id"], ["llm_call_id"], ondelete="SET NULL",
    )
    op.create_check_constraint(
        "ck_task_input_revision_source_llm_call_id_not_blank", "task_input_revision",
        "source_llm_call_id IS NULL OR length(btrim(source_llm_call_id)) > 0",
    )

    op.create_table(
        "natural_language_explanation",
        sa.Column("explanation_id", sa.Text(), primary_key=True),
        sa.Column("task_id", sa.Text(), nullable=False),
        sa.Column("result_id", sa.Text(), nullable=False),
        sa.Column("llm_call_id", sa.Text(), nullable=False),
        sa.Column("attempt_no", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("language", sa.String(32), nullable=False),
        sa.Column("text", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("error_code", sa.String(128), nullable=True),
        sa.Column("safe_error_message", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["task_id"], ["task.task_id"], name="fk_explanation_task", ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["result_id"], ["tool_result.result_id"], name="fk_explanation_result", ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["llm_call_id"], ["llm_call.llm_call_id"], name="fk_explanation_llm_call", ondelete="CASCADE"),
        sa.UniqueConstraint("result_id", "attempt_no", name="uq_explanation_result_attempt"),
        sa.UniqueConstraint("llm_call_id", name="uq_explanation_llm_call_id"),
        sa.CheckConstraint("length(btrim(explanation_id)) > 0", name="ck_explanation_id_not_blank"),
        sa.CheckConstraint("length(btrim(task_id)) > 0", name="ck_explanation_task_id_not_blank"),
        sa.CheckConstraint("length(btrim(result_id)) > 0", name="ck_explanation_result_id_not_blank"),
        sa.CheckConstraint("length(btrim(llm_call_id)) > 0", name="ck_explanation_llm_call_id_not_blank"),
        sa.CheckConstraint("attempt_no > 0", name="ck_explanation_attempt_positive"),
        sa.CheckConstraint("status IN ('PENDING', 'RUNNING', 'SUCCEEDED', 'FAILED')", name="ck_explanation_status_allowed"),
        sa.CheckConstraint("length(btrim(language)) > 0", name="ck_explanation_language_not_blank"),
        sa.CheckConstraint("error_code IS NULL OR (length(btrim(error_code)) > 0 AND length(error_code) <= 64)", name="ck_explanation_error_code_safe"),
        sa.CheckConstraint("safe_error_message IS NULL OR (length(btrim(safe_error_message)) > 0 AND length(safe_error_message) <= 256)", name="ck_explanation_safe_error_message_safe"),
        sa.CheckConstraint("duration_ms IS NULL OR duration_ms >= 0", name="ck_explanation_duration_nonnegative"),
        sa.CheckConstraint("started_at IS NULL OR started_at >= created_at", name="ck_explanation_started_not_before_created"),
        sa.CheckConstraint("completed_at IS NULL OR (started_at IS NOT NULL AND completed_at >= started_at)", name="ck_explanation_completed_not_before_started"),
        sa.CheckConstraint(
            "(status = 'PENDING' AND started_at IS NULL AND completed_at IS NULL "
            "AND duration_ms IS NULL AND text IS NULL AND error_code IS NULL AND safe_error_message IS NULL) OR "
            "(status = 'RUNNING' AND started_at IS NOT NULL AND completed_at IS NULL "
            "AND duration_ms IS NULL AND text IS NULL AND error_code IS NULL AND safe_error_message IS NULL) OR "
            "(status = 'SUCCEEDED' AND started_at IS NOT NULL AND completed_at IS NOT NULL "
            "AND duration_ms IS NOT NULL AND text IS NOT NULL AND length(btrim(text)) > 0 "
            "AND length(text) <= 4096 AND error_code IS NULL AND safe_error_message IS NULL) OR "
            "(status = 'FAILED' AND started_at IS NOT NULL AND completed_at IS NOT NULL "
            "AND duration_ms IS NOT NULL AND text IS NULL AND error_code IS NOT NULL "
            "AND safe_error_message IS NOT NULL)",
            name="ck_explanation_status_shape",
        ),
    )

    op.add_column("idempotency_record", sa.Column("explanation_id", sa.Text(), nullable=True))
    op.create_foreign_key(
        "fk_idempotency_explanation", "idempotency_record", "natural_language_explanation",
        ["explanation_id"], ["explanation_id"], ondelete="CASCADE",
    )
    op.drop_constraint("ck_idempotency_operation_binding", "idempotency_record", type_="check")
    op.drop_constraint("ck_idempotency_operation_allowed", "idempotency_record", type_="check")
    op.create_check_constraint(
        "ck_idempotency_operation_allowed", "idempotency_record",
        "operation IN ('CONVERSATION_CREATE', 'MESSAGE_SUBMIT', 'TASK_CREATE', "
        "'TASK_INPUT_SUPPLEMENT', 'TOOL_RETRY', 'EXPLANATION_RETRY')",
    )
    op.create_check_constraint(
        "ck_idempotency_operation_binding", "idempotency_record",
        "(operation = 'CONVERSATION_CREATE' AND conversation_id IS NOT NULL AND task_id IS NULL "
        "AND message_id IS NULL AND task_input_revision_id IS NULL AND tool_run_id IS NULL AND explanation_id IS NULL) OR "
        "(operation = 'MESSAGE_SUBMIT' AND conversation_id IS NOT NULL AND task_id IS NULL "
        "AND message_id IS NOT NULL AND task_input_revision_id IS NULL AND tool_run_id IS NULL AND explanation_id IS NULL) OR "
        "(operation = 'TASK_CREATE' AND conversation_id IS NOT NULL AND task_id IS NOT NULL "
        "AND message_id IS NOT NULL AND task_input_revision_id IS NULL AND tool_run_id IS NULL AND explanation_id IS NULL) OR "
        "(operation = 'TASK_INPUT_SUPPLEMENT' AND conversation_id IS NOT NULL AND task_id IS NOT NULL "
        "AND message_id IS NOT NULL AND tool_run_id IS NULL AND explanation_id IS NULL) OR "
        "(operation = 'TOOL_RETRY' AND conversation_id IS NULL AND task_id IS NOT NULL "
        "AND message_id IS NULL AND task_input_revision_id IS NULL AND tool_run_id IS NOT NULL AND explanation_id IS NULL) OR "
        "(operation = 'EXPLANATION_RETRY' AND conversation_id IS NULL AND task_id IS NOT NULL "
        "AND message_id IS NULL AND task_input_revision_id IS NULL AND tool_run_id IS NULL AND explanation_id IS NOT NULL)",
    )
