"""Create Conversation, Message, Task, and TaskInputRevision.

Revision ID: 0002_create_conversation_message_task_revision
Revises: 0001_create_actor
"""

from typing import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "0002_create_conversation_message_task_revision"
down_revision: str | None = "0001_create_actor"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column(
        "alembic_version",
        "version_num",
        existing_type=sa.String(length=32),
        type_=sa.String(length=128),
        existing_nullable=False,
    )

    op.create_table(
        "conversation",
        sa.Column("conversation_id", sa.Text(), nullable=False),
        sa.Column("actor_id", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "length(btrim(conversation_id)) > 0",
            name="ck_conversation_conversation_id_not_blank",
        ),
        sa.CheckConstraint(
            "length(btrim(actor_id)) > 0",
            name="ck_conversation_actor_id_not_blank",
        ),
        sa.CheckConstraint(
            "title IS NULL OR length(btrim(title)) > 0",
            name="ck_conversation_title_not_blank",
        ),
        sa.CheckConstraint(
            "updated_at >= created_at",
            name="ck_conversation_updated_at_not_before_created_at",
        ),
        sa.ForeignKeyConstraint(
            ["actor_id"],
            ["actor.actor_id"],
            name="fk_conversation_actor",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("conversation_id", name="pk_conversation"),
    )

    op.create_table(
        "task",
        sa.Column("task_id", sa.Text(), nullable=False),
        sa.Column("conversation_id", sa.Text(), nullable=False),
        sa.Column("actor_id", sa.Text(), nullable=False),
        sa.Column("task_type", sa.String(length=64), nullable=True),
        sa.Column("current_status", sa.String(length=64), nullable=False),
        sa.Column("selected_tool_run_id", sa.Text(), nullable=True),
        sa.Column("selected_result_id", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_code", sa.String(length=128), nullable=True),
        sa.Column("safe_error_message", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "length(btrim(task_id)) > 0",
            name="ck_task_task_id_not_blank",
        ),
        sa.CheckConstraint(
            "length(btrim(conversation_id)) > 0",
            name="ck_task_conversation_id_not_blank",
        ),
        sa.CheckConstraint(
            "length(btrim(actor_id)) > 0",
            name="ck_task_actor_id_not_blank",
        ),
        sa.CheckConstraint(
            "task_type IS NULL OR task_type IN "
            "('KNOWLEDGE_QA', 'TOOL_EXECUTION')",
            name="ck_task_task_type_allowed",
        ),
        sa.CheckConstraint(
            "current_status IN "
            "('PENDING', 'RUNNING', 'NEEDS_INPUT', 'SUCCEEDED', "
            "'PARTIALLY_SUCCEEDED', 'FAILED')",
            name="ck_task_current_status_allowed",
        ),
        sa.CheckConstraint(
            "selected_tool_run_id IS NULL OR "
            "length(btrim(selected_tool_run_id)) > 0",
            name="ck_task_selected_tool_run_id_not_blank",
        ),
        sa.CheckConstraint(
            "selected_result_id IS NULL OR "
            "length(btrim(selected_result_id)) > 0",
            name="ck_task_selected_result_id_not_blank",
        ),
        sa.CheckConstraint(
            "selected_result_id IS NULL OR selected_tool_run_id IS NOT NULL",
            name="ck_task_selected_result_requires_tool_run",
        ),
        sa.CheckConstraint(
            "updated_at >= created_at",
            name="ck_task_updated_at_not_before_created_at",
        ),
        sa.CheckConstraint(
            "completed_at IS NULL OR completed_at >= created_at",
            name="ck_task_completed_at_not_before_created_at",
        ),
        sa.ForeignKeyConstraint(
            ["actor_id"],
            ["actor.actor_id"],
            name="fk_task_actor",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["conversation_id"],
            ["conversation.conversation_id"],
            name="fk_task_conversation",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("task_id", name="pk_task"),
    )

    op.create_table(
        "message",
        sa.Column("message_id", sa.Text(), nullable=False),
        sa.Column("conversation_id", sa.Text(), nullable=False),
        sa.Column("task_id", sa.Text(), nullable=False),
        sa.Column("actor_id", sa.Text(), nullable=False),
        sa.Column("request_id", sa.Text(), nullable=False),
        sa.Column("role", sa.String(length=16), nullable=False),
        sa.Column("generation_source", sa.String(length=16), nullable=False),
        sa.Column("content_text", sa.Text(), nullable=False),
        sa.Column("structured_content", postgresql.JSONB(), nullable=True),
        sa.Column("llm_call_id", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "length(btrim(message_id)) > 0",
            name="ck_message_message_id_not_blank",
        ),
        sa.CheckConstraint(
            "length(btrim(conversation_id)) > 0",
            name="ck_message_conversation_id_not_blank",
        ),
        sa.CheckConstraint(
            "length(btrim(task_id)) > 0",
            name="ck_message_task_id_not_blank",
        ),
        sa.CheckConstraint(
            "length(btrim(actor_id)) > 0",
            name="ck_message_actor_id_not_blank",
        ),
        sa.CheckConstraint(
            "length(btrim(request_id)) > 0",
            name="ck_message_request_id_not_blank",
        ),
        sa.CheckConstraint(
            "role IN ('USER', 'ASSISTANT')",
            name="ck_message_role_allowed",
        ),
        sa.CheckConstraint(
            "generation_source IN ('USER', 'LLM', 'TEMPLATE')",
            name="ck_message_generation_source_allowed",
        ),
        sa.CheckConstraint(
            "length(btrim(content_text)) > 0",
            name="ck_message_content_text_not_blank",
        ),
        sa.CheckConstraint(
            "llm_call_id IS NULL OR length(btrim(llm_call_id)) > 0",
            name="ck_message_llm_call_id_not_blank",
        ),
        sa.CheckConstraint(
            "role <> 'USER' OR "
            "(generation_source = 'USER' AND llm_call_id IS NULL)",
            name="ck_message_user_role_source",
        ),
        sa.CheckConstraint(
            "generation_source <> 'USER' OR role = 'USER'",
            name="ck_message_user_source_role",
        ),
        sa.CheckConstraint(
            "generation_source <> 'LLM' OR "
            "(role = 'ASSISTANT' AND llm_call_id IS NOT NULL)",
            name="ck_message_llm_source_role",
        ),
        sa.CheckConstraint(
            "generation_source <> 'TEMPLATE' OR role = 'ASSISTANT'",
            name="ck_message_template_source_role",
        ),
        sa.ForeignKeyConstraint(
            ["actor_id"],
            ["actor.actor_id"],
            name="fk_message_actor",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["conversation_id"],
            ["conversation.conversation_id"],
            name="fk_message_conversation",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["task_id"],
            ["task.task_id"],
            name="fk_message_task",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("message_id", name="pk_message"),
        sa.UniqueConstraint("llm_call_id", name="uq_message_llm_call_id"),
    )

    op.create_table(
        "task_input_revision",
        sa.Column("task_input_revision_id", sa.Text(), nullable=False),
        sa.Column("task_id", sa.Text(), nullable=False),
        sa.Column("request_id", sa.Text(), nullable=False),
        sa.Column("source_llm_call_id", sa.Text(), nullable=True),
        sa.Column(
            "source_message_ids",
            postgresql.ARRAY(sa.Text()),
            nullable=False,
        ),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("raw_input", postgresql.JSONB(), nullable=False),
        sa.Column("normalized_input", postgresql.JSONB(), nullable=True),
        sa.Column(
            "missing_fields",
            postgresql.ARRAY(sa.Text()),
            nullable=False,
        ),
        sa.Column("ambiguous_fields", postgresql.JSONB(), nullable=False),
        sa.Column("validation_errors", postgresql.JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "length(btrim(task_input_revision_id)) > 0",
            name="ck_task_input_revision_revision_id_not_blank",
        ),
        sa.CheckConstraint(
            "length(btrim(task_id)) > 0",
            name="ck_task_input_revision_task_id_not_blank",
        ),
        sa.CheckConstraint(
            "length(btrim(request_id)) > 0",
            name="ck_task_input_revision_request_id_not_blank",
        ),
        sa.CheckConstraint(
            "source_llm_call_id IS NULL OR "
            "length(btrim(source_llm_call_id)) > 0",
            name="ck_task_input_revision_source_llm_call_id_not_blank",
        ),
        sa.CheckConstraint(
            "revision > 0",
            name="ck_task_input_revision_revision_positive",
        ),
        sa.CheckConstraint(
            "cardinality(source_message_ids) > 0",
            name="ck_task_input_revision_source_message_ids_nonempty",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(raw_input) = 'object'",
            name="ck_task_input_revision_raw_input_object",
        ),
        sa.CheckConstraint(
            "normalized_input IS NULL OR "
            "jsonb_typeof(normalized_input) = 'object'",
            name="ck_task_input_revision_normalized_input_object",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(ambiguous_fields) = 'array'",
            name="ck_task_input_revision_ambiguous_fields_array",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(validation_errors) = 'array'",
            name="ck_task_input_revision_validation_errors_array",
        ),
        sa.ForeignKeyConstraint(
            ["task_id"],
            ["task.task_id"],
            name="fk_task_input_revision_task",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint(
            "task_input_revision_id",
            name="pk_task_input_revision",
        ),
        sa.UniqueConstraint(
            "task_id",
            "revision",
            name="uq_task_input_revision_task_revision",
        ),
    )


def downgrade() -> None:
    op.drop_table("task_input_revision")
    op.drop_table("message")
    op.drop_table("task")
    op.drop_table("conversation")
