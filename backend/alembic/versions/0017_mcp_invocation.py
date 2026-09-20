"""Frozen MCP identity and non-executing receipt reconciliation."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "0017_mcp_invocation"
down_revision = "0016_ebsd_uploaded_assets"
branch_labels = None
depends_on = None


def upgrade():
    for name in ("binding_snapshot", "remote_operation", "remote_receipt"):
        op.add_column("invocation_run", sa.Column(name, JSONB(none_as_null=True), nullable=True))
    op.drop_constraint("ck_invocation_outcome_unknown_profile", "invocation_run", type_="check")
    op.create_check_constraint("ck_invocation_outcome_unknown_profile", "invocation_run",
        "status <> 'OUTCOME_UNKNOWN' OR execution_profile = 'SIDE_EFFECT' OR "
        "(executor_id = 'mcp' AND binding_snapshot IS NOT NULL)")
    op.create_check_constraint("ck_invocation_mcp_facts", "invocation_run",
        "COALESCE((executor_id = 'mcp' AND binding_snapshot IS NOT NULL AND jsonb_typeof(binding_snapshot) = 'object' "
        "AND binding_snapshot->>'executor_id' = 'mcp' AND binding_snapshot->>'tool_id' = tool_id "
        "AND binding_snapshot->>'tool_version' = tool_version AND binding_snapshot->>'schema_hash' = schema_hash "
        "AND (remote_operation IS NULL OR jsonb_typeof(remote_operation) = 'object') "
        "AND (remote_receipt IS NULL OR jsonb_typeof(remote_receipt) = 'object')) OR "
        "(executor_id <> 'mcp' AND binding_snapshot IS NULL AND remote_operation IS NULL AND remote_receipt IS NULL), false)")


def downgrade():
    if op.get_bind().execute(sa.text("SELECT 1 FROM invocation_run WHERE executor_id = 'mcp' LIMIT 1")).first():
        raise RuntimeError("MCP history prevents downgrade; do not discard execution evidence.")
    op.drop_constraint("ck_invocation_mcp_facts", "invocation_run", type_="check")
    op.drop_constraint("ck_invocation_outcome_unknown_profile", "invocation_run", type_="check")
    op.create_check_constraint("ck_invocation_outcome_unknown_profile", "invocation_run",
        "status <> 'OUTCOME_UNKNOWN' OR execution_profile = 'SIDE_EFFECT'")
    for name in ("remote_receipt", "remote_operation", "binding_snapshot"):
        op.drop_column("invocation_run", name)
