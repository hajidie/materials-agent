"""Create operation-scoped idempotency records.

Revision ID: 0008_idempotency_record
Revises: 0007_tool_result_explanation
"""

from typing import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "0008_idempotency_record"
down_revision: str | None = "0007_tool_result_explanation"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "idempotency_record",
        sa.Column("idempotency_record_id", sa.Text(), nullable=False),
        sa.Column("actor_id", sa.Text(), nullable=False),
        sa.Column("operation", sa.String(length=32), nullable=False),
        sa.Column("idempotency_key", sa.String(length=255), nullable=False),
        sa.Column("request_digest", sa.String(length=64), nullable=False),
        sa.Column("first_request_id", sa.Text(), nullable=False),
        sa.Column("task_id", sa.Text(), nullable=True),
        sa.Column("message_id", sa.Text(), nullable=True),
        sa.Column("task_input_revision_id", sa.Text(), nullable=True),
        sa.Column("tool_run_id", sa.Text(), nullable=True),
        sa.Column("explanation_id", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "length(btrim(idempotency_record_id)) > 0",
            name="ck_idempotency_record_id_not_blank",
        ),
        sa.CheckConstraint(
            "length(btrim(actor_id)) > 0",
            name="ck_idempotency_actor_id_not_blank",
        ),
        sa.CheckConstraint(
            "operation IN ('TASK_CREATE', 'TASK_INPUT_SUPPLEMENT', "
            "'TOOL_RETRY', 'EXPLANATION_RETRY')",
            name="ck_idempotency_operation_allowed",
        ),
        sa.CheckConstraint(
            "length(idempotency_key) BETWEEN 1 AND 255 "
            "AND length(btrim(idempotency_key)) > 0 "
            "AND idempotency_key !~ '[[:cntrl:]]'",
            name="ck_idempotency_key_safe",
        ),
        sa.CheckConstraint(
            "request_digest ~ '^[0-9a-f]{64}$'",
            name="ck_idempotency_digest_sha256",
        ),
        sa.CheckConstraint(
            "length(btrim(first_request_id)) > 0",
            name="ck_idempotency_first_request_not_blank",
        ),
        sa.CheckConstraint(
            "(operation = 'TASK_CREATE' AND task_id IS NOT NULL "
            "AND message_id IS NOT NULL AND task_input_revision_id IS NULL "
            "AND tool_run_id IS NULL AND explanation_id IS NULL) OR "
            "(operation = 'TASK_INPUT_SUPPLEMENT' AND task_id IS NOT NULL "
            "AND message_id IS NOT NULL AND tool_run_id IS NULL "
            "AND explanation_id IS NULL) OR "
            "(operation = 'TOOL_RETRY' AND task_id IS NOT NULL "
            "AND message_id IS NULL AND task_input_revision_id IS NULL "
            "AND tool_run_id IS NOT NULL AND explanation_id IS NULL) OR "
            "(operation = 'EXPLANATION_RETRY' AND task_id IS NOT NULL "
            "AND message_id IS NULL AND task_input_revision_id IS NULL "
            "AND tool_run_id IS NULL AND explanation_id IS NOT NULL)",
            name="ck_idempotency_operation_binding",
        ),
        sa.ForeignKeyConstraint(
            ["actor_id"],
            ["actor.actor_id"],
            name="fk_idempotency_actor",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["task_id"],
            ["task.task_id"],
            name="fk_idempotency_task",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["message_id"],
            ["message.message_id"],
            name="fk_idempotency_message",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["task_input_revision_id"],
            ["task_input_revision.task_input_revision_id"],
            name="fk_idempotency_revision",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tool_run_id"],
            ["tool_run.tool_run_id"],
            name="fk_idempotency_tool_run",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["explanation_id"],
            ["natural_language_explanation.explanation_id"],
            name="fk_idempotency_explanation",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint(
            "idempotency_record_id",
            name="pk_idempotency_record",
        ),
        sa.UniqueConstraint(
            "actor_id",
            "operation",
            "idempotency_key",
            name="uq_idempotency_scope_key",
        ),
        sa.UniqueConstraint(
            "first_request_id",
            name="uq_idempotency_first_request",
        ),
    )
    op.create_index(
        "ix_idempotency_scope_created",
        "idempotency_record",
        ["actor_id", "operation", "created_at", "idempotency_record_id"],
    )
    op.create_index(
        "ix_idempotency_task_created",
        "idempotency_record",
        ["task_id", "created_at", "idempotency_record_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_idempotency_task_created",
        table_name="idempotency_record",
    )
    op.drop_index(
        "ix_idempotency_scope_created",
        table_name="idempotency_record",
    )
    op.drop_table("idempotency_record")
