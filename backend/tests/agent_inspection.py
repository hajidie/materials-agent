"""Internal assertions use persistence, not the ordinary chat DTO."""
from materialsagent.infrastructure.db.agent import AgentRunRow


def stored_run(client, view):
    with client.app.state.agent_runtime.store.sessions() as session:
        return session.get(AgentRunRow, view["agent_run_id"]).document
