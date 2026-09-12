"""Bounded AgentRun control plane; no historical business-data backfill.

Revision ID: 0015
Revises: 0014
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0015"
down_revision = "0014_tool_invocation"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("invocation_run", sa.Column("version", sa.Integer(), server_default="0", nullable=False))
    op.drop_index("uq_invocation_message_proposal", table_name="invocation_run")
    op.drop_constraint("ck_message_generation_source_allowed", "message", type_="check")
    op.create_check_constraint("ck_message_generation_source_allowed", "message", "generation_source IN ('USER','LLM','TEMPLATE','AGENT')")
    op.create_check_constraint("ck_message_agent_source_role", "message", "generation_source <> 'AGENT' OR role = 'ASSISTANT'")
    op.create_table("agent_run",
        sa.Column("agent_run_id", sa.Text(), primary_key=True),
        sa.Column("conversation_id", sa.Text(), sa.ForeignKey("conversation.conversation_id", ondelete="CASCADE"), nullable=False),
        sa.Column("actor_id", sa.Text(), sa.ForeignKey("actor.actor_id", ondelete="RESTRICT"), nullable=False),
        sa.Column("source_message_id", sa.Text(), sa.ForeignKey("message.message_id", ondelete="CASCADE"), nullable=False),
        sa.Column("status", sa.String(32), nullable=False), sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("document", postgresql.JSONB(), nullable=False), sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("version >= 0", name="ck_agent_run_version"),
        sa.CheckConstraint("status IN ('PENDING','RUNNING','WAITING_FOR_USER','WAITING_FOR_CONFIRMATION','SUCCEEDED','TERMINATED')", name="ck_agent_run_status"))
    op.create_index("ix_agent_run_conversation_created", "agent_run", ["conversation_id", "created_at", "agent_run_id"])
    op.create_table("agent_submission",
        sa.Column("submission_id", sa.Text(), primary_key=True),
        sa.Column("conversation_id", sa.Text(), sa.ForeignKey("conversation.conversation_id", ondelete="CASCADE"), nullable=False),
        sa.Column("agent_run_id", sa.Text(), sa.ForeignKey("agent_run.agent_run_id", ondelete="CASCADE"), nullable=False),
        sa.Column("message_id", sa.Text(), sa.ForeignKey("message.message_id", ondelete="CASCADE"), nullable=False),
        sa.Column("idempotency_key", sa.Text(), nullable=False), sa.Column("payload_hash", sa.String(64), nullable=False),
        sa.UniqueConstraint("conversation_id", "idempotency_key", name="uq_agent_submission_key"))
    op.create_table("agent_step",
        sa.Column("step_id", sa.Text(), primary_key=True),
        sa.Column("agent_run_id", sa.Text(), sa.ForeignKey("agent_run.agent_run_id", ondelete="CASCADE"), nullable=False),
        sa.Column("number", sa.Integer(), nullable=False), sa.Column("document", postgresql.JSONB(), nullable=False),
        sa.UniqueConstraint("agent_run_id", "number", name="uq_agent_step_number"))
    op.create_table("agent_execution",
        sa.Column("action_id", sa.Text(), sa.ForeignKey("agent_step.step_id", ondelete="CASCADE"), primary_key=True),
        sa.Column("agent_run_id", sa.Text(), sa.ForeignKey("agent_run.agent_run_id", ondelete="CASCADE"), nullable=False),
        sa.Column("invocation_run_id", sa.Text(), sa.ForeignKey("invocation_run.invocation_run_id", ondelete="CASCADE"), nullable=False, unique=True),
        sa.Column("document", postgresql.JSONB(), nullable=False))
    op.create_table("agent_observation",
        sa.Column("observation_id", sa.Text(), primary_key=True),
        sa.Column("agent_run_id", sa.Text(), sa.ForeignKey("agent_run.agent_run_id", ondelete="CASCADE"), nullable=False),
        sa.Column("step_id", sa.Text(), sa.ForeignKey("agent_step.step_id", ondelete="CASCADE"), nullable=False),
        sa.Column("invocation_run_id", sa.Text(), sa.ForeignKey("invocation_run.invocation_run_id", ondelete="CASCADE"), nullable=True),
        sa.Column("document", postgresql.JSONB(), nullable=False),
        sa.UniqueConstraint("agent_run_id", "invocation_run_id", name="uq_agent_observation_invocation"))
    op.create_table("agent_model_call",
        sa.Column("call_id", sa.Text(), primary_key=True),
        sa.Column("agent_run_id", sa.Text(), sa.ForeignKey("agent_run.agent_run_id", ondelete="CASCADE"), nullable=False),
        sa.Column("step_id", sa.Text(), sa.ForeignKey("agent_step.step_id", ondelete="CASCADE"), nullable=False),
        sa.Column("document", postgresql.JSONB(), nullable=False))
    op.create_table("agent_final_answer",
        sa.Column("answer_id", sa.Text(), primary_key=True),
        sa.Column("agent_run_id", sa.Text(), sa.ForeignKey("agent_run.agent_run_id", ondelete="CASCADE"), nullable=False, unique=True),
        sa.Column("step_id", sa.Text(), sa.ForeignKey("agent_step.step_id", ondelete="CASCADE"), nullable=False),
        sa.Column("document", postgresql.JSONB(), nullable=False))


def downgrade():
    op.drop_column("invocation_run", "version")
    # A destructive local upgrade has no business-history rollback contract.
    for table in ["agent_execution", "agent_final_answer", "agent_model_call", "agent_observation", "agent_step", "agent_submission", "agent_run"]:
        op.drop_table(table)
    op.drop_constraint("ck_message_agent_source_role", "message", type_="check")
    op.execute("DELETE FROM message WHERE generation_source = 'AGENT'")
    op.drop_constraint("ck_message_generation_source_allowed", "message", type_="check")
    op.create_check_constraint("ck_message_generation_source_allowed", "message", "generation_source IN ('USER','LLM','TEMPLATE')")
    op.create_index("uq_invocation_message_proposal", "invocation_run", ["source_message_id"], unique=True,
                    postgresql_where=sa.text("trigger = 'MESSAGE_PROPOSAL'"))
