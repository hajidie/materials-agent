"""Replace semantic resource events with call-scoped resource references."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "0021_resource_ref_protocol"
down_revision = "0020_chat_result_events"
branch_labels = depends_on = None


def upgrade():
    op.drop_table("ml_resource_event")
    op.drop_column("conversation", "ml_resource_sequence")


def downgrade():
    op.add_column("conversation", sa.Column("ml_resource_sequence", sa.Integer(), nullable=False, server_default="0"))
    op.create_table("ml_resource_event", sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("actor_id", sa.Text(), nullable=False),
        sa.Column("conversation_id", sa.Text(), sa.ForeignKey("conversation.conversation_id", ondelete="CASCADE"), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False), sa.Column("document", JSONB(), nullable=False),
        sa.Column("event_key", sa.Text(), nullable=False), sa.Column("sequence", sa.Integer(), nullable=False),
        sa.UniqueConstraint("conversation_id", "event_key", name="uq_ml_resource_event_key"),
        sa.UniqueConstraint("conversation_id", "sequence", name="uq_ml_resource_event_sequence"))
    op.execute("""INSERT INTO ml_resource_event (id, actor_id, conversation_id, version, document, event_key, sequence)
        SELECT 'downgrade:' || id, actor_id, conversation_id, 0,
            jsonb_build_object('event_id', 'downgrade:' || id, 'kind', 'SYSTEM_REGISTRATION',
                'resource_kind', resource_type, 'source', jsonb_build_object('source', document->'source'),
                'sequence', row_number() OVER (PARTITION BY conversation_id ORDER BY document->>'created_at', id),
                'reference_id', id, 'created_at', document->'created_at'),
            'registration:' || id, row_number() OVER (PARTITION BY conversation_id ORDER BY document->>'created_at', id)
        FROM ml_resource_ref""")
    op.execute("""UPDATE conversation c SET ml_resource_sequence=e.sequence FROM
        (SELECT conversation_id, max(sequence) AS sequence FROM ml_resource_event GROUP BY conversation_id) e
        WHERE c.conversation_id=e.conversation_id""")
