"""Create the Actor ownership anchor.

Revision ID: 0001_create_actor
Revises:
"""

from typing import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "0001_create_actor"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "actor",
        sa.Column("actor_id", sa.Text(), nullable=False),
        sa.Column("user_id", sa.Text(), nullable=True),
        sa.Column("actor_origin", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.Column(
            "linked_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.CheckConstraint(
            "length(btrim(actor_id)) > 0",
            name="ck_actor_actor_id_not_blank",
        ),
        sa.CheckConstraint(
            "actor_origin = 'LOCAL_ANONYMOUS'",
            name="ck_actor_actor_origin_local_anonymous",
        ),
        sa.PrimaryKeyConstraint("actor_id", name="pk_actor"),
    )


def downgrade() -> None:
    op.drop_table("actor")
