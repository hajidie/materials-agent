"""Independent ML resource schema; frozen DDL, never imports live ORM definitions."""
from alembic import op

revision = "0001_ml_resources"
down_revision = None
branch_labels = None
depends_on = None

BASE = """
 id TEXT PRIMARY KEY, scope_id TEXT NOT NULL CHECK (length(scope_id) BETWEEN 1 AND 128),
 status TEXT NOT NULL, version INTEGER NOT NULL CHECK (version >= 0),
 created_at TIMESTAMPTZ NOT NULL, updated_at TIMESTAMPTZ NOT NULL, data JSON NOT NULL,
 UNIQUE(id, scope_id)
"""
RESOURCE = "CHECK(status IN ('PENDING','AVAILABLE','FAILED','DELETING','DELETED'))"


def upgrade():
    op.execute(f"CREATE TABLE ml_dataset ({BASE}, artifact_id TEXT NOT NULL, {RESOURCE})")
    op.execute("""CREATE TABLE ml_operation (
      auth_domain TEXT NOT NULL, scope_id TEXT NOT NULL, operation TEXT NOT NULL,
      key TEXT NOT NULL, digest TEXT NOT NULL, resource_id TEXT NOT NULL,
      version INTEGER NOT NULL CHECK (version = 0), created_at TIMESTAMPTZ NOT NULL, updated_at TIMESTAMPTZ NOT NULL,
      PRIMARY KEY(auth_domain,scope_id,operation,key))""")
    op.execute(f"""CREATE TABLE ml_training_run ({BASE},
      dataset_id TEXT NOT NULL, model_id TEXT, worker_session TEXT, claim_id TEXT UNIQUE,
      heartbeat_at TIMESTAMPTZ, attempt INTEGER NOT NULL, cancel_requested BOOLEAN NOT NULL,
      recovery_required BOOLEAN NOT NULL, process_stopped BOOLEAN NOT NULL,
      FOREIGN KEY(dataset_id,scope_id) REFERENCES ml_dataset(id,scope_id),
      CHECK ((status = 'SUCCEEDED') = (model_id IS NOT NULL)),
      CHECK (status != 'RUNNING' OR claim_id IS NOT NULL),
      CHECK(status IN ('PENDING','RUNNING','SUCCEEDED','FAILED','CANCELLED')))""")
    op.execute(f"""CREATE TABLE ml_artifact ({BASE},
      dataset_id TEXT, run_id TEXT, member TEXT NOT NULL, object_ref JSON NOT NULL,
      checked_at TIMESTAMPTZ NOT NULL, UNIQUE(run_id,member),
      CHECK ((dataset_id IS NULL) != (run_id IS NULL)),
      FOREIGN KEY(dataset_id,scope_id) REFERENCES ml_dataset(id,scope_id),
      FOREIGN KEY(run_id,scope_id) REFERENCES ml_training_run(id,scope_id), {RESOURCE})""")
    op.execute(f"""CREATE TABLE ml_model ({BASE}, run_id TEXT NOT NULL UNIQUE,
      UNIQUE(id,run_id,scope_id), FOREIGN KEY(run_id,scope_id) REFERENCES ml_training_run(id,scope_id),
      CHECK(status IN ('AVAILABLE')))""")
    op.execute("""ALTER TABLE ml_training_run ADD CONSTRAINT fk_run_published_model
      FOREIGN KEY(model_id,id,scope_id) REFERENCES ml_model(id,run_id,scope_id)""")
    op.execute("""CREATE UNIQUE INDEX uq_ml_active_claim ON ml_training_run(process_stopped)
      WHERE claim_id IS NOT NULL AND process_stopped = false""")
    op.execute("CREATE INDEX ix_ml_queue ON ml_training_run(status,created_at)")
    op.execute("CREATE INDEX ix_ml_artifact_reconcile ON ml_artifact(status,checked_at)")


def downgrade():
    # Never discard scientific records to satisfy a downgrade.
    from sqlalchemy import text
    for table in ("ml_dataset", "ml_training_run", "ml_artifact", "ml_model", "ml_operation"):
        if op.get_bind().execute(text(f"SELECT EXISTS(SELECT 1 FROM {table})")).scalar():
            raise RuntimeError("ML_DOWNGRADE_REQUIRES_EMPTY_DATABASE")
    op.execute("ALTER TABLE ml_training_run DROP CONSTRAINT fk_run_published_model")
    for table in ("ml_model", "ml_artifact", "ml_training_run", "ml_operation", "ml_dataset"):
        op.drop_table(table)
