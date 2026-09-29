"""Replace action-step records with native model/tool call records.

Old Agent conversations must be removed by the scoped chat deletion flow first.
Independent Materials ML records and object-store volumes are untouched.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB


revision = "0024_sdk_agent_loop"
down_revision = "0023_agent_chat_messages"
branch_labels = depends_on = None


def _require_empty_agents() -> None:
    if op.get_bind().scalar(sa.text("SELECT count(*) FROM agent_run")):
        raise RuntimeError("Delete old Agent conversations through scoped cleanup before SDK migration.")


def upgrade() -> None:
    _require_empty_agents()
    op.drop_table("agent_execution")
    op.drop_table("agent_observation")
    op.drop_table("agent_model_call")
    op.drop_table("agent_step")
    op.create_table(
        "agent_execution",
        sa.Column("tool_call_id", sa.Text(), nullable=False),
        sa.Column("agent_run_id", sa.Text(), sa.ForeignKey("agent_run.agent_run_id", ondelete="CASCADE"), nullable=False),
        sa.Column("invocation_run_id", sa.Text(), sa.ForeignKey("invocation_run.invocation_run_id", ondelete="CASCADE"), nullable=False),
        sa.Column("document", JSONB(), nullable=False),
        sa.PrimaryKeyConstraint("tool_call_id", "agent_run_id"),
        sa.UniqueConstraint("invocation_run_id"),
    )
    op.create_table(
        "agent_observation",
        sa.Column("observation_id", sa.Text(), primary_key=True),
        sa.Column("agent_run_id", sa.Text(), sa.ForeignKey("agent_run.agent_run_id", ondelete="CASCADE"), nullable=False),
        sa.Column("tool_call_id", sa.Text(), nullable=False),
        sa.Column("invocation_run_id", sa.Text(), sa.ForeignKey("invocation_run.invocation_run_id", ondelete="CASCADE")),
        sa.Column("document", JSONB(), nullable=False),
        sa.UniqueConstraint("agent_run_id", "invocation_run_id", name="uq_agent_observation_invocation"),
    )
    op.create_table(
        "agent_model_call",
        sa.Column("call_id", sa.Text(), primary_key=True),
        sa.Column("agent_run_id", sa.Text(), sa.ForeignKey("agent_run.agent_run_id", ondelete="CASCADE"), nullable=False),
        sa.Column("document", JSONB(), nullable=False),
    )
    op.create_table(
        "agent_checkpoint_cleanup",
        sa.Column("agent_run_id", sa.Text(), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
    )


def downgrade() -> None:
    _require_empty_agents()
    op.drop_table("agent_checkpoint_cleanup")
    op.drop_table("agent_execution")
    op.drop_table("agent_observation")
    op.drop_table("agent_model_call")
    op.create_table(
        "agent_step",
        sa.Column("step_id", sa.Text(), primary_key=True),
        sa.Column("agent_run_id", sa.Text(), sa.ForeignKey("agent_run.agent_run_id", ondelete="CASCADE"), nullable=False),
        sa.Column("number", sa.Integer(), nullable=False),
        sa.Column("document", JSONB(), nullable=False),
        sa.UniqueConstraint("agent_run_id", "number", name="uq_agent_step_number"),
    )
    op.create_table(
        "agent_execution",
        sa.Column("action_id", sa.Text(), sa.ForeignKey("agent_step.step_id", ondelete="CASCADE"), primary_key=True),
        sa.Column("agent_run_id", sa.Text(), sa.ForeignKey("agent_run.agent_run_id", ondelete="CASCADE"), nullable=False),
        sa.Column("invocation_run_id", sa.Text(), sa.ForeignKey("invocation_run.invocation_run_id", ondelete="CASCADE"), nullable=False, unique=True),
        sa.Column("document", JSONB(), nullable=False),
    )
    op.create_table(
        "agent_observation",
        sa.Column("observation_id", sa.Text(), primary_key=True),
        sa.Column("agent_run_id", sa.Text(), sa.ForeignKey("agent_run.agent_run_id", ondelete="CASCADE"), nullable=False),
        sa.Column("step_id", sa.Text(), sa.ForeignKey("agent_step.step_id", ondelete="CASCADE"), nullable=False),
        sa.Column("invocation_run_id", sa.Text(), sa.ForeignKey("invocation_run.invocation_run_id", ondelete="CASCADE")),
        sa.Column("document", JSONB(), nullable=False),
        sa.UniqueConstraint("agent_run_id", "invocation_run_id", name="uq_agent_observation_invocation"),
    )
    op.create_table(
        "agent_model_call",
        sa.Column("call_id", sa.Text(), primary_key=True),
        sa.Column("agent_run_id", sa.Text(), sa.ForeignKey("agent_run.agent_run_id", ondelete="CASCADE"), nullable=False),
        sa.Column("step_id", sa.Text(), sa.ForeignKey("agent_step.step_id", ondelete="CASCADE"), nullable=False),
        sa.Column("document", JSONB(), nullable=False),
    )
