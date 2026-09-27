"""Canonical chat messages, answer versions, and cancellable Agent turns.

Existing Agent conversations must first be removed through the scoped cleanup.
Downgrade restores schema only, never the intentionally deleted history.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "0023_agent_chat_messages"
down_revision = "0022_retire_legacy_llm_tables"
branch_labels = depends_on = None


def _require_empty_agents():
    if op.get_bind().scalar(sa.text("SELECT count(*) FROM agent_run")):
        raise RuntimeError("Delete existing Agent conversations through the scoped chat cleanup before this migration.")


def upgrade():
    _require_empty_agents()
    op.drop_table("agent_final_answer")
    op.add_column("message", sa.Column("sequence", sa.Integer(), nullable=True))
    op.execute("WITH ordered AS (SELECT message_id, row_number() OVER "
               "(PARTITION BY conversation_id ORDER BY created_at, message_id) AS n FROM message) "
               "UPDATE message SET sequence = ordered.n FROM ordered WHERE message.message_id = ordered.message_id")
    op.alter_column("message", "sequence", nullable=False)
    op.add_column("message", sa.Column("phase", sa.String(16), nullable=False, server_default="notification"))
    op.execute("UPDATE message SET phase = 'user' WHERE role = 'USER'")
    op.add_column("message", sa.Column("agent_run_id", sa.Text(), nullable=True))
    op.add_column("message", sa.Column("answer_root_message_id", sa.Text(), nullable=True))
    op.add_column("message", sa.Column("answer_version", sa.Integer(), nullable=True))
    op.create_foreign_key("fk_message_agent_run", "message", "agent_run", ["agent_run_id"], ["agent_run_id"],
                          ondelete="CASCADE", deferrable=True, initially="DEFERRED")
    op.create_foreign_key("fk_message_answer_root", "message", "message", ["answer_root_message_id", "conversation_id"],
                          ["message_id", "conversation_id"], ondelete="CASCADE", deferrable=True, initially="DEFERRED")
    op.create_unique_constraint("uq_message_conversation_sequence", "message", ["conversation_id", "sequence"])
    op.create_unique_constraint("uq_message_answer_version", "message", ["answer_root_message_id", "answer_version"])
    op.create_check_constraint("ck_message_sequence", "message", "sequence > 0")
    op.create_check_constraint("ck_message_phase", "message", "phase IN ('user','question','answer','notification')")
    op.create_check_constraint("ck_message_answer_version", "message",
        "(answer_root_message_id IS NULL AND answer_version IS NULL) OR "
        "(phase = 'answer' AND role = 'ASSISTANT' AND answer_root_message_id IS NOT NULL AND answer_version > 0)")


def downgrade():
    _require_empty_agents()
    for name in ("ck_message_answer_version", "ck_message_phase", "ck_message_sequence"):
        op.drop_constraint(name, "message", type_="check")
    for name in ("uq_message_answer_version", "uq_message_conversation_sequence"):
        op.drop_constraint(name, "message", type_="unique")
    for name in ("fk_message_answer_root", "fk_message_agent_run"):
        op.drop_constraint(name, "message", type_="foreignkey")
    for name in ("answer_version", "answer_root_message_id", "agent_run_id", "phase", "sequence"):
        op.drop_column("message", name)
    op.create_table("agent_final_answer",
        sa.Column("answer_id", sa.Text(), primary_key=True),
        sa.Column("agent_run_id", sa.Text(), sa.ForeignKey("agent_run.agent_run_id", ondelete="CASCADE"), nullable=False, unique=True),
        sa.Column("step_id", sa.Text(), sa.ForeignKey("agent_step.step_id", ondelete="CASCADE"), nullable=False),
        sa.Column("document", JSONB(), nullable=False))
