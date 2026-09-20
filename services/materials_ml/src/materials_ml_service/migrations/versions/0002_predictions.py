"""Request-owned Prediction lifecycle, preserving the frozen P2 schema."""
from alembic import op
from sqlalchemy import text

revision = "0002_predictions"
down_revision = "0001_ml_resources"
branch_labels = depends_on = None


def upgrade():
    op.execute("""CREATE TABLE ml_prediction (
      id TEXT PRIMARY KEY, scope_id TEXT NOT NULL CHECK(length(scope_id) BETWEEN 1 AND 128),
      status TEXT NOT NULL CHECK(status IN ('PENDING','RUNNING','SUCCEEDED','FAILED','CANCELLED')),
      version INTEGER NOT NULL CHECK(version >= 0), created_at TIMESTAMPTZ NOT NULL,
      updated_at TIMESTAMPTZ NOT NULL, data JSON NOT NULL, UNIQUE(id,scope_id),
      model_id TEXT NOT NULL, input_dataset_id TEXT NOT NULL, artifact_id TEXT,
      cancel_requested BOOLEAN NOT NULL, process_stopped BOOLEAN NOT NULL,
      FOREIGN KEY(model_id,scope_id) REFERENCES ml_model(id,scope_id),
      FOREIGN KEY(input_dataset_id,scope_id) REFERENCES ml_dataset(id,scope_id),
      CHECK((status = 'SUCCEEDED') = (artifact_id IS NOT NULL)),
      CHECK(status NOT IN ('SUCCEEDED','FAILED','CANCELLED') OR process_stopped))""")
    op.execute("ALTER TABLE ml_artifact ADD COLUMN prediction_id TEXT")
    # 0001 used an unnamed owner check. Discover that exact expression, never drop other checks.
    checks = op.get_bind().execute(text("""SELECT conname, pg_get_constraintdef(oid) AS definition
      FROM pg_constraint WHERE conrelid = 'ml_artifact'::regclass AND contype = 'c'""")).mappings()
    owners = [c['conname'] for c in checks if 'dataset_id IS NULL' in c['definition'] and 'run_id IS NULL' in c['definition']]
    if len(owners) != 1:
        raise RuntimeError("ML_OWNER_CONSTRAINT_MISMATCH")
    op.drop_constraint(owners[0], "ml_artifact", type_="check")
    op.execute("""ALTER TABLE ml_artifact
      ADD CONSTRAINT ck_artifact_owner CHECK(num_nonnulls(dataset_id,run_id,prediction_id) = 1),
      ADD CONSTRAINT fk_artifact_prediction FOREIGN KEY(prediction_id,scope_id) REFERENCES ml_prediction(id,scope_id),
      ADD CONSTRAINT uq_prediction_member UNIQUE(prediction_id,member),
      ADD CONSTRAINT uq_prediction_artifact_identity UNIQUE(id,prediction_id,scope_id)""")
    op.execute("""ALTER TABLE ml_prediction ADD CONSTRAINT fk_prediction_result
      FOREIGN KEY(artifact_id,id,scope_id) REFERENCES ml_artifact(id,prediction_id,scope_id)""")


def downgrade():
    if op.get_bind().execute(text("SELECT EXISTS(SELECT 1 FROM ml_prediction)")).scalar():
        raise RuntimeError("ML_DOWNGRADE_REQUIRES_EMPTY_PREDICTIONS")
    op.drop_constraint("fk_prediction_result", "ml_prediction", type_="foreignkey")
    for name, kind in (("fk_artifact_prediction", "foreignkey"), ("uq_prediction_member", "unique"),
                       ("uq_prediction_artifact_identity", "unique"), ("ck_artifact_owner", "check")):
        op.drop_constraint(name, "ml_artifact", type_=kind)
    op.drop_column("ml_artifact", "prediction_id")
    op.create_check_constraint("ck_artifact_owner", "ml_artifact", "(dataset_id IS NULL) != (run_id IS NULL)")
    op.drop_table("ml_prediction")
