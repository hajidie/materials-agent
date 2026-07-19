"""Add Task time ordering constraints.

Revision ID: 0003_task_time_order
Revises: 0002_create_conversation_message_task_revision
"""

from typing import Sequence

from alembic import op


revision: str = "0003_task_time_order"
down_revision: str | None = "0002_create_conversation_message_task_revision"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_check_constraint(
        "ck_task_started_at_not_before_created_at",
        "task",
        "started_at IS NULL OR started_at >= created_at",
    )
    op.create_check_constraint(
        "ck_task_completed_at_not_before_started_at",
        "task",
        "completed_at IS NULL "
        "OR started_at IS NULL "
        "OR completed_at >= started_at",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_task_completed_at_not_before_started_at",
        "task",
        type_="check",
    )
    op.drop_constraint(
        "ck_task_started_at_not_before_created_at",
        "task",
        type_="check",
    )
