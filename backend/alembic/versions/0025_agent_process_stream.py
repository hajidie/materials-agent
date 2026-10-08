"""Public research process snapshots and recoverable interrupted runs."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0025_agent_process_stream"
down_revision = "0024_sdk_agent_loop"
branch_labels = None
depends_on = None


def upgrade():
    op.drop_constraint("ck_agent_run_status", "agent_run", type_="check")
    op.create_check_constraint("ck_agent_run_status", "agent_run",
        "status IN ('PENDING','RUNNING','WAITING_FOR_USER','WAITING_FOR_CONFIRMATION','INTERRUPTED','SUCCEEDED','TERMINATED')")
    op.create_table("agent_process",
        sa.Column("agent_run_id", sa.Text(), sa.ForeignKey("agent_run.agent_run_id", ondelete="CASCADE"), primary_key=True),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("document", postgresql.JSONB(), nullable=False))


def downgrade():
    connection = op.get_bind()
    if connection.scalar(sa.text("SELECT count(*) FROM agent_run WHERE status = 'INTERRUPTED'")):
        raise RuntimeError("Resolve interrupted runs before downgrading.")
    op.drop_table("agent_process")
    op.drop_constraint("ck_agent_run_status", "agent_run", type_="check")
    op.create_check_constraint("ck_agent_run_status", "agent_run",
        "status IN ('PENDING','RUNNING','WAITING_FOR_USER','WAITING_FOR_CONFIRMATION','SUCCEEDED','TERMINATED')")
