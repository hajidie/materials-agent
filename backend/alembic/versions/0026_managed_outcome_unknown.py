"""Preserve uncertain Managed Runtime outcomes for receipt reconciliation."""
from alembic import op
import sqlalchemy as sa

revision = "0026_managed_outcome_unknown"
down_revision = "0025_agent_process_stream"
branch_labels = None
depends_on = None


def upgrade():
    op.drop_constraint("ck_invocation_outcome_unknown_profile", "invocation_run", type_="check")
    op.create_check_constraint("ck_invocation_outcome_unknown_profile", "invocation_run",
        "status <> 'OUTCOME_UNKNOWN' OR execution_profile IN ('SIDE_EFFECT', 'MANAGED') OR "
        "(executor_id = 'mcp' AND binding_snapshot IS NOT NULL)")


def downgrade():
    if op.get_bind().execute(sa.text("SELECT 1 FROM invocation_run WHERE status = 'OUTCOME_UNKNOWN' "
            "AND execution_profile = 'MANAGED' AND executor_id <> 'mcp' LIMIT 1")).first():
        raise RuntimeError("Resolve unknown Managed outcomes before downgrading; preserve execution evidence.")
    op.drop_constraint("ck_invocation_outcome_unknown_profile", "invocation_run", type_="check")
    op.create_check_constraint("ck_invocation_outcome_unknown_profile", "invocation_run",
        "status <> 'OUTCOME_UNKNOWN' OR execution_profile = 'SIDE_EFFECT' OR "
        "(executor_id = 'mcp' AND binding_snapshot IS NOT NULL)")
