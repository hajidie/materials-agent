from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import Mock
import pytest
from sqlalchemy import select
from materialsagent.application.chat_upgrade import inventory
from materialsagent.application.conversation_cleanup import ConversationCleanupService
from materialsagent.infrastructure.db.agent import AgentRunRow, SQLAlchemyAgentStore
from materialsagent.infrastructure.db.conversation_task import MessageRow
from materialsagent.domain.ports.agent import AgentFailure
from backend.tests.api.test_chat_artifacts import chat


def test_upgrade_inventory_rejects_old_history_without_reading_compatibility(chat):
    service, remote, conversation, run, ref = chat
    assert inventory(service.sessions, 'agent-test') == []
    with service.sessions.begin() as session:
        message = session.get(MessageRow, run['source_message_id'])
        message.structured_content = None
    targets = inventory(service.sessions, 'agent-test')
    assert len(targets) == 1 and targets[0]['conversation_id'] == conversation
    assert targets[0]['blocked'] == [] and targets[0]['ml_references'] == 1
    with pytest.raises(AgentFailure, match='CONVERSATION_UPGRADE_REQUIRED'):
        SQLAlchemyAgentStore(service.sessions).submit(conversation, 'agent-test', 'new request', 'upgrade-gate')
    with service.sessions.begin() as session:
        row = session.get(AgentRunRow, run['agent_run_id'])
        document = dict(row.document)
        document['executions'] = [{**document['executions'][0], 'status': 'OUTCOME_UNKNOWN'}]
        row.document = document
    assert inventory(service.sessions, 'agent-test')[0]['blocked'] == ['BUSY_OR_UNKNOWN_AGENT']
    assert inventory(service.sessions, 'other-actor') == []


def test_explicit_upgrade_cleanup_never_consumes_unrelated_pending_work():
    repository = Mock()
    repository.get.side_effect = lambda identity: SimpleNamespace(cleanup_id=identity, status='PENDING')
    @contextmanager
    def uow():
        yield SimpleNamespace(conversation_object_cleanups=repository)
    service = object.__new__(ConversationCleanupService)
    service._unit_of_work_factory = uow
    service._attempt = Mock(return_value='COMPLETED')
    result = service.drain_exact(('selected', 'selected'))
    assert result.attempted == result.completed == 1
    repository.get.assert_called_once_with('selected')
    repository.list_pending.assert_not_called()
