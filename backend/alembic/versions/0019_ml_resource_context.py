"""Stable dataset ordinals and attributable resource events."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "0019_ml_resource_context"
down_revision = "0018_ml_resources"
branch_labels = depends_on = None


def upgrade():
    op.add_column("conversation", sa.Column("ml_dataset_ordinal", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("conversation", sa.Column("ml_resource_sequence", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("ml_resource_ref", sa.Column("dataset_ordinal", sa.Integer(), nullable=True))
    op.create_unique_constraint("uq_ml_dataset_ordinal", "ml_resource_ref", ["conversation_id", "dataset_ordinal"])
    op.execute("""WITH numbered AS (
        SELECT id, row_number() OVER (PARTITION BY conversation_id ORDER BY document->>'created_at', id)::int AS ordinal
        FROM ml_resource_ref WHERE resource_type='dataset')
        UPDATE ml_resource_ref r SET dataset_ordinal=n.ordinal,
        document=r.document || jsonb_build_object('dataset_ordinal', n.ordinal, 'ordinal_source', 'migration-registration-order')
        FROM numbered n WHERE r.id=n.id""")
    op.execute("""UPDATE conversation c SET ml_dataset_ordinal=n.ordinal FROM
        (SELECT conversation_id, max(dataset_ordinal) AS ordinal FROM ml_resource_ref GROUP BY conversation_id) n
        WHERE c.conversation_id=n.conversation_id AND n.ordinal IS NOT NULL""")
    op.create_table("ml_resource_event", sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("actor_id", sa.Text(), nullable=False),
        sa.Column("conversation_id", sa.Text(), sa.ForeignKey("conversation.conversation_id", ondelete="CASCADE"), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False), sa.Column("document", JSONB(), nullable=False),
        sa.Column("event_key", sa.Text(), nullable=False), sa.Column("sequence", sa.Integer(), nullable=False),
        sa.UniqueConstraint("conversation_id", "event_key", name="uq_ml_resource_event_key"),
        sa.UniqueConstraint("conversation_id", "sequence", name="uq_ml_resource_event_sequence"))
    op.execute("""INSERT INTO ml_resource_event (id, actor_id, conversation_id, version, document, event_key, sequence)
        SELECT 'migration:' || id, actor_id, conversation_id, 0,
            jsonb_build_object('event_id', 'migration:' || id, 'kind', 'SYSTEM_REGISTRATION',
                'resource_kind', resource_type, 'source', jsonb_build_object('source', document->'source'),
                'sequence', row_number() OVER (PARTITION BY conversation_id ORDER BY document->>'created_at', id),
                'reference_id', id, 'created_at', document->'created_at'),
            'registration:' || id, row_number() OVER (PARTITION BY conversation_id ORDER BY document->>'created_at', id)
        FROM ml_resource_ref""")
    op.execute("""UPDATE conversation c SET ml_resource_sequence=e.sequence FROM
        (SELECT conversation_id, max(sequence) AS sequence FROM ml_resource_event GROUP BY conversation_id) e
        WHERE c.conversation_id=e.conversation_id""")


def downgrade():
    op.drop_table("ml_resource_event")
    op.drop_constraint("uq_ml_dataset_ordinal", "ml_resource_ref", type_="unique")
    op.drop_column("ml_resource_ref", "dataset_ordinal")
    op.drop_column("conversation", "ml_resource_sequence")
    op.drop_column("conversation", "ml_dataset_ordinal")
