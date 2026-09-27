"""Internal assertions use persistence, not the ordinary chat DTO."""
from materialsagent.infrastructure.db.agent import AgentRunRow


def stored_run(client, view):
    with client.app.state.agent_runtime.store.sessions() as session:
        document = dict(session.get(AgentRunRow, view["agent_run_id"]).document)
        run = client.app.state.agent_runtime.store.get(view["agent_run_id"], session.get(AgentRunRow, view["agent_run_id"]).actor_id)
        document["user_inputs"] = run.user_inputs
        document["user_messages"] = run.user_messages
        document["context"] = run.context
        document["waiting"] = run.waiting.model_dump() if run.waiting else None
        final = next((m for m in run.messages if m["message_id"] == run.final_message_id), None)
        document["final_answer"] = {"answer_id": final["message_id"], "text": final["text"]} if final else None
        return document
