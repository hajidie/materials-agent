"""Neutral scope closure and durable cleanup ownership."""
from alembic import op
import sqlalchemy as sa

revision = "0003_scopes"
down_revision = "0002_predictions"
branch_labels = depends_on = None


def upgrade():
    op.execute("""CREATE TABLE ml_scope (
      scope_id TEXT PRIMARY KEY CHECK(length(scope_id) BETWEEN 1 AND 128),
      status TEXT NOT NULL CHECK(status IN ('OPEN','CLOSED')), version INTEGER NOT NULL CHECK(version >= 0),
      closed_by TEXT, cleanup_pending BOOLEAN NOT NULL, created_at TIMESTAMPTZ NOT NULL,
      updated_at TIMESTAMPTZ NOT NULL, CHECK((status='CLOSED') = (closed_by IS NOT NULL)))""")
    op.execute("""CREATE TABLE ml_scope_close (
      scope_id TEXT NOT NULL REFERENCES ml_scope(scope_id), operation_id TEXT NOT NULL,
      status TEXT NOT NULL CHECK(status IN ('CLOSED','BUSY')), created_at TIMESTAMPTZ NOT NULL,
      PRIMARY KEY(scope_id,operation_id))""")
    op.execute("""INSERT INTO ml_scope SELECT scope_id,'OPEN',0,NULL,false,now(),now() FROM (
      SELECT scope_id FROM ml_dataset UNION SELECT scope_id FROM ml_training_run
      UNION SELECT scope_id FROM ml_prediction UNION SELECT scope_id FROM ml_model) s""")


def downgrade():
    if op.get_bind().execute(sa.text("SELECT 1 FROM ml_scope_close LIMIT 1")).first():
        raise RuntimeError("ML_SCOPE_HISTORY_PREVENTS_DOWNGRADE")
    op.drop_table("ml_scope_close")
    op.drop_table("ml_scope")
