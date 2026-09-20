"""Explicit aggregate cleanup for the chat contract; not a compatibility reader."""
from sqlalchemy import select, func
from materialsagent.infrastructure.db.agent import AgentRunRow
from materialsagent.infrastructure.db.conversation_task import ConversationRow, MessageRow, TaskRow
from materialsagent.infrastructure.db.asset import AssetRow
from materialsagent.infrastructure.db.ml_resources import references, uploads


def inventory(sessions, actor):
    result = []
    with sessions() as session:
        conversations = session.scalars(select(ConversationRow).where(ConversationRow.actor_id == actor)).all()
        for conversation in conversations:
            identity = conversation.conversation_id
            messages = session.scalars(select(MessageRow.structured_content).where(MessageRow.conversation_id == identity)).all()
            runs = session.scalars(select(AgentRunRow.document).where(AgentRunRow.conversation_id == identity)).all()
            refs = session.scalars(select(references.c.document).where(references.c.conversation_id == identity)).all()
            incompatible = any(not message or message.get("contract") != "chat-v1" for message in messages)
            incompatible |= any("user_messages" not in run or
                run.get("resource_protocol_version") != "resource-ref-v1" for run in runs)
            incompatible |= bool(refs and not messages)
            if not incompatible:
                continue
            blocked = []
            if conversation.deletion_fence_operation_id:
                blocked.append("DELETION_UNRESOLVED")
            if any(run["status"] in ("PENDING", "RUNNING", "WAITING_FOR_CONFIRMATION") or
                any(e.get("status") == "OUTCOME_UNKNOWN" for e in run.get("executions", [])) for run in runs):
                blocked.append("BUSY_OR_UNKNOWN_AGENT")
            if session.scalar(select(func.count()).select_from(TaskRow).where(TaskRow.conversation_id == identity,
                TaskRow.current_status == "RUNNING")):
                blocked.append("BUSY_TASK")
            if session.scalar(select(func.count()).select_from(uploads).where(uploads.c.conversation_id == identity,
                uploads.c.status != "RESOLVED")):
                blocked.append("UNKNOWN_UPLOAD")
            if any(ref.get("actor_id") != actor or ref.get("conversation_id") != identity for ref in refs):
                blocked.append("OWNERSHIP_UNCONFIRMED")
            result.append({"conversation_id": identity, "messages": len(messages), "agent_runs": len(runs),
                "ml_references": len(refs), "assets": session.scalar(select(func.count()).select_from(AssetRow).where(AssetRow.conversation_id == identity)),
                "blocked": blocked})
    return result
