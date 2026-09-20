"""Append-only conversation result publications."""
from alembic import op
import sqlalchemy as sa

revision = "0020_chat_result_events"
down_revision = "0019_ml_resource_context"
branch_labels = depends_on = None


def upgrade():
    op.add_column("message", sa.Column("event_key", sa.Text(), nullable=True))
    op.create_unique_constraint("uq_message_conversation_event", "message", ["conversation_id", "event_key"])


def downgrade():
    op.drop_constraint("uq_message_conversation_event", "message", type_="unique")
    op.drop_column("message", "event_key")
