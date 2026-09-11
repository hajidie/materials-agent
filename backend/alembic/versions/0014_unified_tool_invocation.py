"""Add the unified Tool invocation control plane.

Revision ID: 0014_tool_invocation
Revises: 0013_conversation_delete
"""

from __future__ import annotations

from typing import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "0014_tool_invocation"
down_revision: str | None = "0013_conversation_delete"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint("ck_idempotency_operation_allowed", "idempotency_record", type_="check")
    op.drop_constraint("ck_idempotency_operation_binding", "idempotency_record", type_="check")
    op.create_check_constraint(
        "ck_idempotency_operation_allowed",
        "idempotency_record",
        "operation IN ('CONVERSATION_CREATE', 'MESSAGE_SUBMIT', 'TASK_CREATE', "
        "'TASK_INPUT_SUPPLEMENT', 'TOOL_RETRY', 'EXPLANATION_RETRY')",
    )
    op.create_check_constraint(
        "ck_idempotency_operation_binding",
        "idempotency_record",
        "(operation = 'CONVERSATION_CREATE' AND conversation_id IS NOT NULL AND task_id IS NULL AND message_id IS NULL AND task_input_revision_id IS NULL AND tool_run_id IS NULL AND explanation_id IS NULL) OR "
        "(operation = 'MESSAGE_SUBMIT' AND conversation_id IS NOT NULL AND task_id IS NULL AND message_id IS NOT NULL AND task_input_revision_id IS NULL AND tool_run_id IS NULL AND explanation_id IS NULL) OR "
        "(operation = 'TASK_CREATE' AND conversation_id IS NOT NULL AND task_id IS NOT NULL AND message_id IS NOT NULL AND task_input_revision_id IS NULL AND tool_run_id IS NULL AND explanation_id IS NULL) OR "
        "(operation = 'TASK_INPUT_SUPPLEMENT' AND conversation_id IS NOT NULL AND task_id IS NOT NULL AND message_id IS NOT NULL AND tool_run_id IS NULL AND explanation_id IS NULL) OR "
        "(operation = 'TOOL_RETRY' AND conversation_id IS NULL AND task_id IS NOT NULL AND message_id IS NULL AND task_input_revision_id IS NULL AND tool_run_id IS NOT NULL AND explanation_id IS NULL) OR "
        "(operation = 'EXPLANATION_RETRY' AND conversation_id IS NULL AND task_id IS NOT NULL AND message_id IS NULL AND task_input_revision_id IS NULL AND tool_run_id IS NULL AND explanation_id IS NOT NULL)",
    )
    op.drop_constraint("ck_message_task_id_not_blank", "message", type_="check")
    op.alter_column("message", "task_id", existing_type=sa.Text(), nullable=True)
    op.create_check_constraint(
        "ck_message_task_id_not_blank",
        "message",
        "task_id IS NULL OR length(btrim(task_id)) > 0",
    )
    op.create_unique_constraint(
        "uq_message_id_conversation",
        "message",
        ["message_id", "conversation_id"],
    )

    op.drop_constraint("ck_llm_call_task_id_not_blank", "llm_call", type_="check")
    op.alter_column("llm_call", "task_id", existing_type=sa.Text(), nullable=True)
    op.create_check_constraint(
        "ck_llm_call_task_id_not_blank",
        "llm_call",
        "task_id IS NULL OR length(btrim(task_id)) > 0",
    )
    op.add_column("llm_call", sa.Column("source_message_id", sa.Text(), nullable=True))
    op.execute(
        """
        UPDATE llm_call AS call
        SET source_message_id = (
            SELECT message.message_id
            FROM message
            WHERE message.task_id = call.task_id
              AND message.conversation_id = call.conversation_id
              AND message.role = 'USER'
            ORDER BY
                (message.request_id = call.request_id) DESC,
                message.created_at ASC,
                message.message_id ASC
            LIMIT 1
        )
        """
    )
    op.alter_column("llm_call", "source_message_id", existing_type=sa.Text(), nullable=False)
    op.create_check_constraint(
        "ck_llm_call_source_message_not_blank",
        "llm_call",
        "length(btrim(source_message_id)) > 0",
    )
    op.create_foreign_key(
        "fk_llm_call_source_message_conversation",
        "llm_call",
        "message",
        ["source_message_id", "conversation_id"],
        ["message_id", "conversation_id"],
        ondelete="CASCADE",
    )
    op.create_index(
        "ix_llm_call_source_message_created",
        "llm_call",
        ["source_message_id", "created_at", "llm_call_id"],
    )

    op.create_unique_constraint(
        "uq_tool_run_id_task",
        "tool_run",
        ["tool_run_id", "task_id"],
    )

    op.create_table(
        "invocation_result",
        sa.Column("invocation_result_id", sa.Text(), nullable=False),
        sa.Column("invocation_run_id", sa.Text(), nullable=False),
        sa.Column("data", postgresql.JSONB(), nullable=False),
        sa.Column("presentation", postgresql.JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "length(btrim(invocation_result_id)) > 0",
            name="ck_invocation_result_id_not_blank",
        ),
        sa.CheckConstraint(
            "length(btrim(invocation_run_id)) > 0",
            name="ck_invocation_result_run_id_not_blank",
        ),
        sa.CheckConstraint("jsonb_typeof(data) = 'object'", name="ck_invocation_result_data_object"),
        sa.CheckConstraint(
            "jsonb_typeof(presentation) = 'object'",
            name="ck_invocation_result_presentation_object",
        ),
        sa.PrimaryKeyConstraint("invocation_result_id"),
    )

    op.create_table(
        "invocation_run",
        sa.Column("invocation_run_id", sa.Text(), nullable=False),
        sa.Column("actor_id", sa.Text(), nullable=False),
        sa.Column("conversation_id", sa.Text(), nullable=False),
        sa.Column("source_message_id", sa.Text(), nullable=False),
        sa.Column("task_id", sa.Text(), nullable=True),
        sa.Column("retry_of_invocation_run_id", sa.Text(), nullable=True),
        sa.Column("request_id", sa.Text(), nullable=False),
        sa.Column("idempotency_key", sa.Text(), nullable=False),
        sa.Column("trigger", sa.String(length=32), nullable=False),
        sa.Column("tool_id", sa.Text(), nullable=False),
        sa.Column("tool_version", sa.String(length=64), nullable=False),
        sa.Column("schema_hash", sa.String(length=64), nullable=False),
        sa.Column("execution_profile", sa.String(length=32), nullable=False),
        sa.Column("executor_id", sa.String(length=64), nullable=False),
        sa.Column("proposed_arguments", postgresql.JSONB(), nullable=False),
        sa.Column("policy_snapshot", postgresql.JSONB(), nullable=False),
        sa.Column("tool_projection", postgresql.JSONB(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("confirmation_required", sa.Boolean(), nullable=False),
        sa.Column("confirmation_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("confirmed_by", sa.Text(), nullable=True),
        sa.Column("rejected_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("rejected_by", sa.Text(), nullable=True),
        sa.Column("expired_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("authorization_checked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("denied_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("execution_claim_token", sa.Text(), nullable=True),
        sa.Column("execution_lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("dispatch_started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("execution_attempt_count", sa.Integer(), nullable=False),
        sa.Column("invocation_result_id", sa.Text(), nullable=True),
        sa.Column("managed_tool_run_id", sa.Text(), nullable=True),
        sa.Column("error_code", sa.String(length=128), nullable=True),
        sa.Column("safe_error_message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "trigger IN ('MESSAGE_PROPOSAL', 'EXPLICIT_RETRY')",
            name="ck_invocation_trigger_allowed",
        ),
        sa.CheckConstraint(
            "execution_profile IN ('STANDARD', 'SIDE_EFFECT', 'MANAGED')",
            name="ck_invocation_profile_allowed",
        ),
        sa.CheckConstraint(
            "status IN ('PENDING', 'PENDING_CONFIRMATION', 'RUNNING', 'SUCCEEDED', "
            "'FAILED', 'DENIED', 'REJECTED', 'EXPIRED', 'OUTCOME_UNKNOWN')",
            name="ck_invocation_status_allowed",
        ),
        sa.CheckConstraint("schema_hash ~ '^[0-9a-f]{64}$'", name="ck_invocation_schema_hash"),
        sa.CheckConstraint(
            "jsonb_typeof(proposed_arguments) = 'object' AND jsonb_typeof(policy_snapshot) = 'object'",
            name="ck_invocation_json_objects",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(tool_projection) = 'object'",
            name="ck_invocation_tool_projection_object",
        ),
        sa.CheckConstraint(
            "(execution_profile = 'MANAGED' AND task_id IS NOT NULL AND invocation_result_id IS NULL) OR "
            "(execution_profile <> 'MANAGED' AND task_id IS NULL AND managed_tool_run_id IS NULL)",
            name="ck_invocation_profile_relationship",
        ),
        sa.CheckConstraint(
            "(execution_profile = 'SIDE_EFFECT') = confirmation_required",
            name="ck_invocation_profile_confirmation",
        ),
        sa.CheckConstraint(
            "NOT (invocation_result_id IS NOT NULL AND managed_tool_run_id IS NOT NULL)",
            name="ck_invocation_result_relationship_exclusive",
        ),
        sa.CheckConstraint(
            "confirmation_required OR (confirmation_expires_at IS NULL AND confirmed_at IS NULL "
            "AND confirmed_by IS NULL AND rejected_at IS NULL AND rejected_by IS NULL AND expired_at IS NULL)",
            name="ck_invocation_confirmation_scope",
        ),
        sa.CheckConstraint(
            "NOT confirmation_required OR confirmation_expires_at IS NOT NULL",
            name="ck_invocation_confirmation_expiry_required",
        ),
        sa.CheckConstraint(
            "(confirmed_at IS NULL) = (confirmed_by IS NULL) AND "
            "(rejected_at IS NULL) = (rejected_by IS NULL)",
            name="ck_invocation_confirmation_fact_pairs",
        ),
        sa.CheckConstraint(
            "NOT (confirmed_at IS NOT NULL AND rejected_at IS NOT NULL)",
            name="ck_invocation_confirmation_facts_exclusive",
        ),
        sa.CheckConstraint(
            "status <> 'PENDING_CONFIRMATION' OR "
            "(confirmation_required AND confirmed_at IS NULL AND rejected_at IS NULL AND expired_at IS NULL)",
            name="ck_invocation_pending_confirmation_shape",
        ),
        sa.CheckConstraint(
            "status <> 'REJECTED' OR rejected_at IS NOT NULL",
            name="ck_invocation_rejected_facts",
        ),
        sa.CheckConstraint(
            "status <> 'EXPIRED' OR expired_at IS NOT NULL",
            name="ck_invocation_expired_facts",
        ),
        sa.CheckConstraint(
            "status <> 'DENIED' OR denied_at IS NOT NULL",
            name="ck_invocation_denied_facts",
        ),
        sa.CheckConstraint(
            "status <> 'RUNNING' OR (execution_claim_token IS NOT NULL AND execution_lease_expires_at IS NOT NULL)",
            name="ck_invocation_running_claim",
        ),
        sa.CheckConstraint(
            "status <> 'OUTCOME_UNKNOWN' OR execution_profile = 'SIDE_EFFECT'",
            name="ck_invocation_outcome_unknown_profile",
        ),
        sa.CheckConstraint(
            "dispatch_started_at IS NULL OR "
            "(execution_attempt_count > 0 AND execution_claim_token IS NOT NULL)",
            name="ck_invocation_dispatch_attempt",
        ),
        sa.CheckConstraint(
            "execution_attempt_count >= 0",
            name="ck_invocation_attempt_count",
        ),
        sa.CheckConstraint(
            "(status IN ('SUCCEEDED', 'FAILED', 'DENIED', 'REJECTED', 'EXPIRED', 'OUTCOME_UNKNOWN')) "
            "= (completed_at IS NOT NULL)",
            name="ck_invocation_terminal_completion",
        ),
        sa.CheckConstraint(
            "status <> 'SUCCEEDED' OR "
            "((execution_profile = 'MANAGED' AND managed_tool_run_id IS NOT NULL) OR "
            "(execution_profile <> 'MANAGED' AND invocation_result_id IS NOT NULL))",
            name="ck_invocation_success_result_required",
        ),
        sa.CheckConstraint(
            "invocation_result_id IS NULL OR status = 'SUCCEEDED'",
            name="ck_invocation_result_terminal",
        ),
        sa.CheckConstraint("updated_at >= created_at", name="ck_invocation_updated_order"),
        sa.ForeignKeyConstraint(
            ["actor_id"], ["actor.actor_id"], name="fk_invocation_actor", ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["conversation_id"],
            ["conversation.conversation_id"],
            name="fk_invocation_conversation",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["source_message_id", "conversation_id"],
            ["message.message_id", "message.conversation_id"],
            name="fk_invocation_source_message_conversation",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["task_id"], ["task.task_id"], name="fk_invocation_task", ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["retry_of_invocation_run_id"],
            ["invocation_run.invocation_run_id"],
            name="fk_invocation_retry_of",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["invocation_result_id"],
            ["invocation_result.invocation_result_id"],
            name="fk_invocation_result",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["managed_tool_run_id", "task_id"],
            ["tool_run.tool_run_id", "tool_run.task_id"],
            name="fk_invocation_managed_tool_run_task",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("invocation_run_id"),
        sa.UniqueConstraint("invocation_result_id", name="uq_invocation_result"),
        sa.UniqueConstraint(
            "actor_id", "idempotency_key", name="uq_invocation_actor_idempotency"
        ),
    )
    op.create_index(
        "uq_invocation_message_proposal",
        "invocation_run",
        ["source_message_id"],
        unique=True,
        postgresql_where=sa.text("trigger = 'MESSAGE_PROPOSAL'"),
    )
    op.create_index(
        "ix_invocation_conversation_created",
        "invocation_run",
        ["conversation_id", "created_at", "invocation_run_id"],
    )
    op.create_index(
        "ix_invocation_recovery",
        "invocation_run",
        ["status", "execution_lease_expires_at"],
    )
    op.create_foreign_key(
        "fk_invocation_result_run",
        "invocation_result",
        "invocation_run",
        ["invocation_run_id"],
        ["invocation_run_id"],
        ondelete="CASCADE",
    )
    op.create_unique_constraint(
        "uq_invocation_result_run",
        "invocation_result",
        ["invocation_run_id"],
    )


def downgrade() -> None:
    op.drop_constraint(
        "uq_invocation_result_run",
        "invocation_result",
        type_="unique",
    )
    op.drop_constraint(
        "fk_invocation_result_run",
        "invocation_result",
        type_="foreignkey",
    )
    op.drop_index("ix_invocation_recovery", table_name="invocation_run")
    op.drop_index("ix_invocation_conversation_created", table_name="invocation_run")
    op.drop_index("uq_invocation_message_proposal", table_name="invocation_run")
    op.drop_table("invocation_run")
    op.drop_table("invocation_result")
    op.drop_constraint("uq_tool_run_id_task", "tool_run", type_="unique")
    op.drop_index("ix_llm_call_source_message_created", table_name="llm_call")
    op.drop_constraint(
        "fk_llm_call_source_message_conversation", "llm_call", type_="foreignkey"
    )
    op.drop_constraint("ck_llm_call_source_message_not_blank", "llm_call", type_="check")
    op.drop_column("llm_call", "source_message_id")
    op.drop_constraint("ck_llm_call_task_id_not_blank", "llm_call", type_="check")
    op.alter_column("llm_call", "task_id", existing_type=sa.Text(), nullable=False)
    op.create_check_constraint(
        "ck_llm_call_task_id_not_blank",
        "llm_call",
        "length(btrim(task_id)) > 0",
    )
    op.drop_constraint("uq_message_id_conversation", "message", type_="unique")
    op.drop_constraint("ck_message_task_id_not_blank", "message", type_="check")
    op.alter_column("message", "task_id", existing_type=sa.Text(), nullable=False)
    op.create_check_constraint(
        "ck_message_task_id_not_blank",
        "message",
        "length(btrim(task_id)) > 0",
    )
    op.drop_constraint("ck_idempotency_operation_binding", "idempotency_record", type_="check")
    op.drop_constraint("ck_idempotency_operation_allowed", "idempotency_record", type_="check")
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
