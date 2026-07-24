"""Add Conversation timeline query indexes.

Revision ID: 0009_timeline_query_indexes
Revises: 0008_idempotency_record
"""

from typing import Sequence

from alembic import op


revision: str = "0009_timeline_query_indexes"
down_revision: str | None = "0008_idempotency_record"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        "ix_message_conversation_created",
        "message",
        ["conversation_id", "created_at", "message_id"],
    )
    op.create_index(
        "ix_task_conversation_created",
        "task",
        ["conversation_id", "created_at", "task_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_task_conversation_created",
        table_name="task",
    )
    op.drop_index(
        "ix_message_conversation_created",
        table_name="message",
    )
