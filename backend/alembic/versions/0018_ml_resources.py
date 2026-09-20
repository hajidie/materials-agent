"""Controlled references and durable conversation deletion fence."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB
from hashlib import sha256
import json
from uuid import uuid4

revision = "0018_ml_resources"
down_revision = "0017_mcp_invocation"
branch_labels = depends_on = None


def upgrade():
    op.add_column("conversation", sa.Column("deletion_fence_operation_id", sa.Text(), nullable=True))
    op.add_column("conversation", sa.Column("deletion_fence_version", sa.Integer(), nullable=False, server_default="0"))
    for name, persistent, extra in (
        ("ml_scope_binding", False, [sa.Column("service_id", sa.Text(), nullable=False), sa.UniqueConstraint("conversation_id", "service_id", name="uq_ml_scope_binding")]),
        ("ml_resource_ref", False, [sa.Column("service_id", sa.Text(), nullable=False), sa.Column("resource_type", sa.Text(), nullable=False),
            sa.Column("resource_id", sa.Text(), nullable=False), sa.UniqueConstraint("service_id", "conversation_id", "resource_type", "resource_id", name="uq_ml_resource_ref")]),
        ("ml_resource_operation", True, [sa.Column("operation_key", sa.Text(), nullable=False), sa.Column("status", sa.Text(), nullable=False),
            sa.UniqueConstraint("actor_id", "conversation_id", "operation_key", name="uq_ml_upload_key")]),
        ("conversation_deletion", True, [sa.Column("status", sa.Text(), nullable=False)])):
        op.create_table(name, sa.Column("id", sa.Text(), primary_key=True), sa.Column("actor_id", sa.Text(), nullable=False),
            sa.Column("conversation_id", sa.Text(), *(() if persistent else (sa.ForeignKey("conversation.conversation_id", ondelete="CASCADE"),)), nullable=False),
            sa.Column("version", sa.Integer(), nullable=False), sa.Column("document", JSONB(), nullable=False), *extra)
    settings = op.get_context().config.attributes.get("settings")
    if settings is not None:
        from materialsagent.application.materials_ml_tools import build_ml_tools
        from materialsagent.application.tool_registry import ToolRegistry
        endpoint = sha256(settings.materials_ml_mcp_url.encode()).hexdigest()
        registry = ToolRegistry(build_ml_tools(binding_version=settings.materials_ml_binding_version, endpoint_digest=endpoint))
        approved = {tool.tool_id: tool.binding.execution_target.snapshot(tool) for tool in registry.list_registered()}
        rows = op.get_bind().execute(sa.text("""SELECT i.*, r.data AS result_data, a.conversation_id AS agent_conversation,
            a.actor_id AS agent_actor, m.conversation_id AS message_conversation, m.actor_id AS message_actor
            FROM invocation_run i JOIN conversation c ON c.conversation_id=i.conversation_id AND c.actor_id=i.actor_id
            JOIN message m ON m.message_id=i.source_message_id
            JOIN agent_execution e ON e.invocation_run_id=i.invocation_run_id
            JOIN agent_run a ON a.agent_run_id=e.agent_run_id
            LEFT JOIN invocation_result r ON r.invocation_result_id=i.invocation_result_id AND r.invocation_run_id=i.invocation_run_id
            WHERE i.executor_id='mcp'""")).mappings()
        for row in rows:
            service = historical_binding(row, approved)
            if service:
                op.get_bind().execute(sa.text("""INSERT INTO ml_scope_binding
                    (id,actor_id,conversation_id,version,service_id,document)
                    VALUES (:id,:actor,:conversation,0,:service_id,CAST(:document AS jsonb))
                    ON CONFLICT (conversation_id,service_id) DO NOTHING"""), {
                        "id": uuid4().hex, "actor": row["actor_id"], "conversation": row["conversation_id"],
                        "service_id": service["service_id"], "document": json.dumps({"scope_id": row["conversation_id"],
                            "service": service, "source": "verified-history:" + row["invocation_run_id"]})})


def historical_binding(row, approved):
    """No remote IO, string-only ownership inference or reconstruction of historical digests."""
    binding = row.get("binding_snapshot")
    if not binding or approved.get(row["tool_id"]) != binding or row.get("dispatch_started_at") is None:
        return None
    if (row["agent_actor"] != row["actor_id"] or row["message_actor"] != row["actor_id"]
            or row["agent_conversation"] != row["conversation_id"] or row["message_conversation"] != row["conversation_id"]):
        return None
    receipt = row.get("remote_receipt") or {}
    resource = (row.get("result_data") or {}).get("resource") if row["status"] == "SUCCEEDED" else None
    if not resource:
        operation = row.get("remote_operation") or {}
        if receipt.get("lookup_status") != "FOUND" or not operation or any(receipt.get(k) != operation.get(k)
                for k in ("operation", "request_digest", "digest_version")):
            return None
        resource = receipt.get("resource")
    if not isinstance(resource, dict) or resource.get("scope_id") != row["conversation_id"] or not resource.get("id"):
        return None
    return {"service_id": binding["server_id"], "binding_version": binding["binding_version"], "endpoint_digest": binding["endpoint_digest"]}


def downgrade():
    if op.get_bind().execute(sa.text("SELECT 1 FROM conversation_deletion UNION ALL SELECT 1 FROM ml_resource_operation LIMIT 1")).first():
        raise RuntimeError("Resource coordination history prevents downgrade.")
    for name in ("conversation_deletion", "ml_resource_operation", "ml_resource_ref", "ml_scope_binding"):
        op.drop_table(name)
    op.drop_column("conversation", "deletion_fence_version")
    op.drop_column("conversation", "deletion_fence_operation_id")
