"""Add bounded Conversation Context audit snapshots.

Revision ID: 0012_llm_context_snapshot
Revises: 0011_tool_run_provenance
"""

from __future__ import annotations

from typing import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "0012_llm_context_snapshot"
down_revision: str | None = "0011_tool_run_provenance"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "llm_call",
        sa.Column(
            "context_snapshot",
            postgresql.JSONB(),
            nullable=True,
        ),
    )
    op.create_check_constraint(
        "ck_llm_call_context_snapshot_object",
        "llm_call",
        "context_snapshot IS NULL OR jsonb_typeof(context_snapshot) = 'object'",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_llm_call_context_snapshot_object",
        "llm_call",
        type_="check",
    )
    op.drop_column("llm_call", "context_snapshot")
