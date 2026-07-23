from __future__ import annotations

from sqlalchemy import func, select, text

from materialsagent.application.chat_orchestration import (
    ChatOrchestrationService,
)
from materialsagent.infrastructure.db.conversation_task import (
    MessageRow,
    TaskInputRevisionRow,
    TaskRow,
)
from materialsagent.infrastructure.db.llm_call import LLMCallRow
from materialsagent.infrastructure.db.session import create_session_factory
from materialsagent.infrastructure.llm.mock import (
    MockChatOrchestrationAdapter,
    default_mock_responder,
)
from backend.tests.api.test_assets import _MemoryStorage


def _create_conversation(client) -> str:
    response = client.post("/api/v1/conversations", json={})
    assert response.status_code == 201
    return response.json()["data"]["conversation_id"]


def _submit(client, conversation_id: str, content_text: str):
    return client.post(
        f"/api/v1/conversations/{conversation_id}/messages",
        json={
            "submission_mode": "NEW_TASK",
            "content_text": content_text,
        },
    )


class _CountingStorage(_MemoryStorage):
    def __init__(self) -> None:
        super().__init__()
        self.calls = 0
        self.put_calls = 0

    def put(self, object_key, payload, content_type, metadata=None):
        self.calls += 1
        self.put_calls += 1
        return super().put(object_key, payload, content_type, metadata)

    def head(self, object_key):
        self.calls += 1
        return super().head(object_key)

    def get(self, object_key, *, max_bytes):
        self.calls += 1
        return super().get(object_key, max_bytes=max_bytes)

    def delete(self, object_key):
        self.calls += 1
        return super().delete(object_key)


def _rows(api_harness) -> tuple[list[MessageRow], list[TaskRow], list[TaskInputRevisionRow], list[LLMCallRow]]:
    session_factory = create_session_factory(api_harness.engine)
    with session_factory() as session:
        return (
            list(session.scalars(select(MessageRow)).all()),
            list(session.scalars(select(TaskRow)).all()),
            list(session.scalars(select(TaskInputRevisionRow)).all()),
            list(session.scalars(select(LLMCallRow)).all()),
        )


def test_knowledge_message_returns_only_committed_assistant_and_succeeded_task(
    api_harness,
) -> None:
    actor_id = "actor_local"
    api_harness.persist_actor(actor_id)

    with api_harness.create_client(actor_id) as client:
        conversation_id = _create_conversation(client)
        response = _submit(client, conversation_id, "什么是 ZTA35G？")

    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"request_id", "data"}
    data = body["data"]
    assert data["conversation_id"] == conversation_id
    assert data["task"]["task_type"] == "KNOWLEDGE_QA"
    assert data["task"]["status"] == "SUCCEEDED"
    assert data["task"]["selected_tool_run_id"] is None
    assert data["task"]["selected_result_id"] is None
    assert data["assistant_message"]["role"] == "ASSISTANT"
    assert data["assistant_message"]["content_text"]
    assert data["needs_input"] is None
    assert data["result_summary"] is None
    assert data["explanation"] is None
    messages, tasks, revisions, calls = _rows(api_harness)
    assert len(messages) == 2
    assert len(tasks) == 1
    assert revisions == []
    assert len(calls) == 1
    assert calls[0].status == "SUCCEEDED"
    assert messages[1].llm_call_id == calls[0].llm_call_id
    assert data["assistant_message"]["message_id"] == messages[1].message_id


def test_missing_and_ambiguous_messages_return_formal_needs_input_projection(
    api_harness,
) -> None:
    actor_id = "actor_local"
    api_harness.persist_actor(actor_id)

    with api_harness.create_client(actor_id) as client:
        missing_conversation = _create_conversation(client)
        missing = _submit(
            client,
            missing_conversation,
            "缺 aging_temperature",
        )
        ambiguous_conversation = _create_conversation(client)
        ambiguous = _submit(client, ambiguous_conversation, "明确歧义参数")

    assert missing.status_code == ambiguous.status_code == 200
    missing_data = missing.json()["data"]
    assert missing_data["task"]["task_type"] == "TOOL_EXECUTION"
    assert missing_data["task"]["status"] == "NEEDS_INPUT"
    assert missing_data["needs_input"]["missing_fields"] == [
        "aging_temperature"
    ]
    assert missing_data["needs_input"]["ambiguous_fields"] == []
    assert missing_data["needs_input"]["normalized_input"][
        "solution_time"
    ] == {"value": 3, "unit": "h"}
    assert missing_data["assistant_message"]["content_text"]
    ambiguous_data = ambiguous.json()["data"]
    assert ambiguous_data["task"]["status"] == "NEEDS_INPUT"
    assert ambiguous_data["needs_input"]["missing_fields"] == []
    assert ambiguous_data["needs_input"]["ambiguous_fields"] == [
        {"field": "solution_time", "candidates": [2, 3]}
    ]


def test_hard_invalid_message_returns_422_with_persisted_failed_resource(
    api_harness,
) -> None:
    actor_id = "actor_local"
    api_harness.persist_actor(actor_id)

    with api_harness.create_client(actor_id) as client:
        conversation_id = _create_conversation(client)
        response = _submit(client, conversation_id, "越界温度")

    assert response.status_code == 422
    body = response.json()
    assert body["error"]["code"] == "VALIDATION_FAILED"
    assert body["error"]["details"] == [
        {
            "field": "solution_temperature",
            "code": "PROCESS_PARAMETERS_OUT_OF_RANGE",
            "message": "The value is outside the supported inclusive range.",
        }
    ]
    assert body["resource"]["conversation_id"] == conversation_id
    assert body["resource"]["task_id"]
    messages, tasks, revisions, calls = _rows(api_harness)
    assert len(messages) == 1
    assert len(revisions) == 1
    assert calls[0].status == "SUCCEEDED"
    assert tasks[0].current_status == "FAILED"
    assert tasks[0].error_code == "VALIDATION_FAILED"


def test_complete_valid_tool_message_returns_persisted_503_without_fake_result(
    api_harness,
) -> None:
    actor_id = "actor_local"
    api_harness.persist_actor(actor_id)

    with api_harness.create_client(actor_id) as client:
        conversation_id = _create_conversation(client)
        response = _submit(client, conversation_id, "完整合法 Tool 请求")
        task_response = client.get(
            f"/api/v1/tasks/{response.json()['resource']['task_id']}"
        )

    assert response.status_code == 503
    body = response.json()
    assert body["error"] == {
        "code": "TOOL_UNAVAILABLE",
        "message": "当前阶段尚未开放材料工具执行。",
        "details": [],
    }
    assert body["resource"]["conversation_id"] == conversation_id
    assert body["resource"]["task_id"]
    assert body["resource"]["tool_run_id"] is None
    assert body["resource"]["result_id"] is None
    assert task_response.status_code == 200
    task_data = task_response.json()["data"]
    assert task_data["task_type"] == "TOOL_EXECUTION"
    assert task_data["status"] == "FAILED"
    assert task_data["error_code"] == "TOOL_UNAVAILABLE"
    assert task_data["selected_tool_run_id"] is None
    assert task_data["selected_result_id"] is None
    messages, tasks, revisions, calls = _rows(api_harness)
    assert len(messages) == 1
    assert len(revisions) == 1
    assert calls[0].status == "SUCCEEDED"
    assert tasks[0].error_code == "TOOL_UNAVAILABLE"
    with api_harness.engine.connect() as connection:
        assert connection.scalar(text("SELECT count(*) FROM tool_run")) == 0
        assert connection.scalar(text("SELECT to_regclass('public.asset')")) == "asset"
        assert connection.scalar(text("SELECT count(*) FROM asset")) == 0
        assert connection.scalar(
            text("SELECT to_regclass('public.tool_result')")
        ) == "tool_result"
        assert connection.scalar(text("SELECT count(*) FROM tool_result")) == 0
        assert connection.scalar(
            text("SELECT count(*) FROM natural_language_explanation")
        ) == 0


def test_enabled_m7_without_runtime_boundary_has_zero_tool_side_effects(
    api_harness,
) -> None:
    actor_id = "actor_local"
    api_harness.persist_actor(actor_id)
    storage = _CountingStorage()
    runtime_missing_settings = api_harness.settings.model_copy(
        update={
            "zta35g_runtime_url": None,
            "zta35g_runtime_token": None,
        }
    )

    with api_harness.create_client(
        actor_id,
        settings=runtime_missing_settings,
        storage_service=storage,
        m7_tool_chain_enabled=True,
    ) as client:
        assert client.app.state.tool_workflow_service is None
        assert storage.calls == 0
        conversation_id = _create_conversation(client)
        response = _submit(client, conversation_id, "完整合法 Tool 请求")

    assert response.status_code == 503
    body = response.json()
    assert body["error"]["code"] == "TOOL_UNAVAILABLE"
    assert body["resource"]["tool_run_id"] is None
    assert body["resource"]["result_id"] is None
    assert storage.calls == 0
    assert storage.put_calls == 0
    assert storage.objects == {}
    messages, tasks, revisions, calls = _rows(api_harness)
    assert len(messages) == 1
    assert len(tasks) == 1
    assert len(revisions) == 1
    assert len(calls) == 1
    assert tasks[0].current_status == "FAILED"
    assert tasks[0].error_code == "TOOL_UNAVAILABLE"
    assert tasks[0].selected_tool_run_id is None
    assert tasks[0].selected_result_id is None
    with api_harness.engine.connect() as connection:
        assert connection.scalar(text("SELECT count(*) FROM tool_run")) == 0
        assert connection.scalar(text("SELECT count(*) FROM asset")) == 0
        assert connection.scalar(text("SELECT count(*) FROM tool_result")) == 0
        assert connection.scalar(
            text("SELECT count(*) FROM natural_language_explanation")
        ) == 0
        assert connection.scalar(
            text(
                "SELECT count(*) FROM llm_call "
                "WHERE purpose='TOOL_RESULT_EXPLANATION'"
            )
        ) == 0


def test_injected_enabled_chat_service_is_disabled_without_complete_workflow(
    api_harness,
) -> None:
    actor_id = "actor_local"
    api_harness.persist_actor(actor_id)
    injected = ChatOrchestrationService(
        api_harness.unit_of_work_factory,
        MockChatOrchestrationAdapter(default_mock_responder),
        tool_chain_enabled=True,
    )
    incomplete_settings = api_harness.settings.model_copy(
        update={
            "minio_endpoint": None,
            "minio_access_key": None,
            "minio_secret_key": None,
            "minio_bucket": None,
            "minio_secure": None,
        }
    )

    with api_harness.create_client(
        actor_id,
        settings=incomplete_settings,
        chat_orchestration_service=injected,
        tool_workflow_service=None,
        asset_service=None,
        storage_service=None,
        m7_tool_chain_enabled=True,
    ) as client:
        conversation_id = _create_conversation(client)
        response = _submit(client, conversation_id, "完整合法 Tool 请求")

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "TOOL_UNAVAILABLE"
    with api_harness.engine.connect() as connection:
        assert connection.scalar(text("SELECT count(*) FROM tool_run")) == 0
        assert connection.scalar(text("SELECT count(*) FROM asset")) == 0
        assert connection.scalar(text("SELECT count(*) FROM tool_result")) == 0
        assert connection.scalar(
            text("SELECT count(*) FROM natural_language_explanation")
        ) == 0


def test_solution_time_minutes_are_visible_only_as_committed_revision_on_503(
    api_harness,
) -> None:
    actor_id = "actor_local"
    api_harness.persist_actor(actor_id)

    with api_harness.create_client(actor_id) as client:
        conversation_id = _create_conversation(client)
        response = _submit(client, conversation_id, "solution_time = 180 min")

    assert response.status_code == 503
    _, _, revisions, _ = _rows(api_harness)
    assert revisions[0].raw_input["parameters"]["solution_time"] == {
        "value": 180,
        "unit": "min",
    }
    assert revisions[0].normalized_input["solution_time"] == {
        "value": 3,
        "unit": "h",
    }


def test_timeout_provider_and_protocol_errors_use_safe_persisted_envelopes(
    api_harness,
) -> None:
    actor_id = "actor_local"
    api_harness.persist_actor(actor_id)

    scenarios = (
        ("Mock timeout", 504, "UPSTREAM_TIMEOUT"),
        ("Mock provider failure", 503, "CHAT_ORCHESTRATION_FAILED"),
        ("Mock protocol failure", 502, "CHAT_ORCHESTRATION_FAILED"),
    )
    with api_harness.create_client(actor_id) as client:
        responses = []
        for content_text, _, _ in scenarios:
            conversation_id = _create_conversation(client)
            responses.append(_submit(client, conversation_id, content_text))

    for response, (_, status_code, code) in zip(responses, scenarios, strict=True):
        assert response.status_code == status_code
        body = response.json()
        assert body["error"]["code"] == code
        assert body["resource"]["conversation_id"]
        assert body["resource"]["task_id"]
        assert body["resource"]["tool_run_id"] is None
        assert body["resource"]["result_id"] is None
        assert "Traceback" not in response.text
        assert "provider_payload" not in response.text
        assert "SECRET" not in response.text
        assert "C:\\" not in response.text
    messages, tasks, revisions, calls = _rows(api_harness)
    assert len(messages) == 3
    assert revisions == []
    assert all(task.current_status == "FAILED" for task in tasks)
    assert all(call.status == "FAILED" for call in calls)
    assert all(call.structured_output_summary is None for call in calls)


def test_api_can_inject_a_controlled_chat_port_without_calling_it_at_app_creation(
    api_harness,
) -> None:
    actor_id = "actor_local"
    api_harness.persist_actor(actor_id)
    calls = 0

    def responder(_input: object) -> dict[str, object]:
        nonlocal calls
        calls += 1
        return {"route": "KNOWLEDGE_ANSWER", "answer_text": "注入回答。"}

    port = MockChatOrchestrationAdapter(responder)
    with api_harness.create_client(
        actor_id,
        chat_orchestration_port=port,
    ) as client:
        assert calls == 0
        conversation_id = _create_conversation(client)
        assert calls == 0
        response = _submit(client, conversation_id, "任意文本")

    assert response.status_code == 200
    assert response.json()["data"]["assistant_message"]["content_text"] == "注入回答。"
    assert calls == 1
