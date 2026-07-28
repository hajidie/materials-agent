from __future__ import annotations

from sqlalchemy import func, select

from backend.tests.api.conftest import api_harness, api_postgres_settings
from backend.tests.api.test_assets import _MemoryStorage
from backend.tests.api.test_m8_explanation_retry import (
    _Runtime,
    _client_options,
    _create_explanation_failure,
)
from materialsagent.infrastructure.db.conversation_task import (
    MessageRow,
    TaskRow,
)
from materialsagent.infrastructure.db.llm_call import LLMCallRow
from materialsagent.infrastructure.db.session import create_session_factory
from materialsagent.infrastructure.llm.mock import (
    MockChatOrchestrationAdapter,
    default_mock_responder,
)
from materialsagent.infrastructure.llm.mock_explanation import (
    MockExplanationAdapter,
)


class CountingResponder:
    def __init__(self) -> None:
        self.call_count = 0

    def __call__(self, value):
        self.call_count += 1
        return default_mock_responder(value)


def test_chat_idempotency_replay_does_not_call_provider_twice(
    api_harness,
) -> None:
    actor_id = "actor_m12a_chat_idempotency"
    api_harness.persist_actor(actor_id)
    responder = CountingResponder()
    port = MockChatOrchestrationAdapter(responder)

    with api_harness.create_client(
        actor_id,
        chat_orchestration_port=port,
    ) as client:
        conversation_id = client.post(
            "/api/v1/conversations",
            json={},
        ).json()["data"]["conversation_id"]
        request = {
            "submission_mode": "NEW_TASK",
            "content_text": "什么是 ZTA35G？",
        }
        first = client.post(
            f"/api/v1/conversations/{conversation_id}/messages",
            headers={"Idempotency-Key": "m12a-chat-idempotency"},
            json=request,
        )
        replay = client.post(
            f"/api/v1/conversations/{conversation_id}/messages",
            headers={"Idempotency-Key": "m12a-chat-idempotency"},
            json=request,
        )

    assert first.status_code == replay.status_code == 200
    assert first.json()["data"]["idempotency_replayed"] is False
    assert replay.json()["data"]["idempotency_replayed"] is True
    assert responder.call_count == 1
    session_factory = create_session_factory(api_harness.engine)
    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(LLMCallRow)) == 1
        assert session.scalar(select(func.count()).select_from(TaskRow)) == 1
        assert session.scalar(select(func.count()).select_from(MessageRow)) == 2


def test_explanation_retry_replay_and_new_key_have_exact_call_counts(
    api_harness,
) -> None:
    actor_id = "actor_m12a_explanation_idempotency"
    api_harness.persist_actor(actor_id)
    runtime = _Runtime()
    explanation = MockExplanationAdapter(mode="failure")
    with api_harness.create_client(
        actor_id,
        **_client_options(
            api_harness,
            runtime,
            _MemoryStorage(),
            explanation,
        ),
    ) as client:
        initial = _create_explanation_failure(client)
        result_id = initial["result_summary"]["result_id"]
        assert explanation.call_count == 1
        explanation.mode = "timeout"
        endpoint = f"/api/v1/tool-results/{result_id}/explanations"
        first = client.post(
            endpoint,
            headers={"Idempotency-Key": "m12a-explanation-retry-1"},
            json={},
        )
        assert explanation.call_count == 2
        replay = client.post(
            endpoint,
            headers={"Idempotency-Key": "m12a-explanation-retry-1"},
            json={},
        )
        assert explanation.call_count == 2
        new_key = client.post(
            endpoint,
            headers={"Idempotency-Key": "m12a-explanation-retry-2"},
            json={},
        )

    assert first.status_code == replay.status_code == new_key.status_code == 200
    assert first.json()["data"]["idempotency_replayed"] is False
    assert replay.json()["data"]["idempotency_replayed"] is True
    assert new_key.json()["data"]["idempotency_replayed"] is False
    assert explanation.call_count == 3
    assert runtime.calls == 1
    session_factory = create_session_factory(api_harness.engine)
    with session_factory() as session:
        explanation_calls = session.scalar(
            select(func.count())
            .select_from(LLMCallRow)
            .where(LLMCallRow.purpose == "TOOL_RESULT_EXPLANATION")
        )
    assert explanation_calls == 3
