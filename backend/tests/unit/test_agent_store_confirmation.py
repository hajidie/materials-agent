from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from backend.tests.agent_state import agent_run
from materialsagent.domain.models.agent import ExecutionRecord
from materialsagent.domain.ports.agent import AgentConflictError
from materialsagent.infrastructure.db.agent import SQLAlchemyAgentStore


@pytest.fixture
def confirmation_store(monkeypatch):
    run = agent_run(conversation_id="conversation", actor_id="actor", source_message_id="message",
        status="RUNNING", waiting_version=1, version=7,
        pending_execution=ExecutionRecord(tool_call_id="call", tool_name="convert", version="1",
            schema_hash="a" * 64, arguments={"value": 2}, execution_fingerprint="b" * 64,
            invocation_run_id="invocation", confirmation_version="confirmation", confirmed=True,
            dispatched=True, status="RUNNING"))
    row = SimpleNamespace(actor_id=run.actor_id, conversation_id=run.conversation_id,
        version=run.version, status=run.status, document=run.model_dump(mode="json"))
    sessions = MagicMock()
    session = sessions.begin.return_value.__enter__.return_value
    session.get.return_value = row
    session.scalar.return_value = row
    monkeypatch.setattr("materialsagent.infrastructure.db.ml_resources.lock_conversation", MagicMock())
    store = SQLAlchemyAgentStore(sessions)
    monkeypatch.setattr(store, "_hydrate", lambda _session, current: current)
    return store, run, row


@pytest.mark.parametrize("status", ["RUNNING", "INTERRUPTED"])
def test_accepted_confirmation_replay_returns_current_run_without_writing(confirmation_store, status):
    store, run, row = confirmation_store
    row.status = row.document["status"] = status
    before = deepcopy(row.document)

    current = store.accept_confirmation(run.agent_run_id, run.actor_id, 1, "confirmation", True)

    assert current.status == status and current.version == 7
    assert current.pending_execution.confirmed and current.pending_execution.dispatched
    assert row.document == before and row.version == 7


@pytest.mark.parametrize("waiting_version,confirmation_version,approved", [
    (1, "confirmation", False),
    (2, "confirmation", True),
    (1, "other-confirmation", True),
])
def test_confirmation_replay_rejects_changed_decision_or_target(
    confirmation_store, waiting_version, confirmation_version, approved,
):
    store, run, row = confirmation_store
    before = deepcopy(row.document)
    with pytest.raises(AgentConflictError):
        store.accept_confirmation(run.agent_run_id, run.actor_id, waiting_version, confirmation_version, approved)
    assert row.document == before and row.version == 7


def test_queued_confirmation_replay_still_checks_confirmation_version(confirmation_store):
    store, run, row = confirmation_store
    row.document.update(status="PENDING", confirmation_response={
        "tool_call_id": "call", "waiting_version": 1, "approved": True,
    })
    row.document["pending_execution"].update(confirmed=False, dispatched=False, status="PENDING_CONFIRMATION")
    with pytest.raises(AgentConflictError):
        store.accept_confirmation(run.agent_run_id, run.actor_id, 1, "other-confirmation", True)
    current = store.accept_confirmation(run.agent_run_id, run.actor_id, 1, "confirmation", True)
    assert current.status == "PENDING" and current.version == 7
