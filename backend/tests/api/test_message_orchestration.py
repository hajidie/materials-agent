from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import func, select, text

from materialsagent.application.chat_orchestration import (
    ChatOrchestrationProjection,
    ChatOrchestrationService,
)
from materialsagent.application.context import ActorContext
from materialsagent.application.errors import ApplicationInternalError
from materialsagent.application.messages import PreparedSubmission
from materialsagent.api.routes.conversations import (
    MessageSubmissionRequest,
    submit_message,
)
from materialsagent.domain.models.message import Message
from materialsagent.domain.models.task import Task
from materialsagent.domain.models.task_input_revision import TaskInputRevision
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


ROUTE_TIME = datetime(2026, 9, 2, 9, 0, tzinfo=timezone.utc)
ROUTE_SCHEMA_HASH = (
    "f821240f782ce788bc723fd1acd02a2e58cedbf68b70b1414e2accd16d989d07"
)


def _ready_route_projection(
    *,
    submission_mode: str,
    include_revision: bool = True,
) -> tuple[PreparedSubmission, ChatOrchestrationProjection]:
    task = Task(
        task_id="task_route",
        conversation_id="conversation_route",
        actor_id="actor_route",
        task_type="TOOL_EXECUTION",
        current_status="READY",
        selected_tool_run_id=None,
        selected_result_id=None,
        created_at=ROUTE_TIME,
        started_at=ROUTE_TIME,
        updated_at=ROUTE_TIME,
        completed_at=None,
        error_code=None,
        safe_error_message=None,
        tool_id="zta35g_sem_virtual_lab",
        bound_tool_version="1",
        bound_schema_hash=ROUTE_SCHEMA_HASH,
    )
    message = Message.user(
        message_id="message_route",
        conversation_id=task.conversation_id,
        task_id=task.task_id,
        actor_id=task.actor_id,
        request_id="request_route",
        content_text="complete tool input",
        created_at=ROUTE_TIME,
    )
    revision = (
        None
        if not include_revision
        else TaskInputRevision(
            task_input_revision_id="revision_route",
            task_id=task.task_id,
            request_id=message.request_id,
            source_llm_call_id="llm_route",
            source_message_ids=[message.message_id],
            revision=(2 if submission_mode == "SUPPLEMENT_TASK" else 1),
            raw_input={"material": "ZTA35G"},
            normalized_input={"material": "ZTA35G"},
            missing_fields=[],
            ambiguous_fields=[],
            validation_errors=[],
            created_at=ROUTE_TIME,
        )
    )
    submission = PreparedSubmission(
        conversation_id=task.conversation_id,
        user_message=message,
        task=task,
        submission_mode=submission_mode,
    )
    return submission, ChatOrchestrationProjection(
        conversation_id=task.conversation_id,
        user_message=message,
        task=task,
        llm_call=None,
        assistant_message=None,
        revision=revision,
    )


class _DirectSubmissionService:
    def __init__(self, submission: PreparedSubmission) -> None:
        self.submission = submission

    def prepare_submission(self, *_args, **_kwargs) -> PreparedSubmission:
        return self.submission


class _DirectOrchestrationService:
    def __init__(self, projection: ChatOrchestrationProjection) -> None:
        self.projection = projection

    def orchestrate_submission(
        self,
        *_args,
        **_kwargs,
    ) -> ChatOrchestrationProjection:
        return self.projection


class _WorkflowInvoked(RuntimeError):
    pass


class _DirectWorkflowService:
    def execute(self, actor: ActorContext, **kwargs):
        raise _WorkflowInvoked((actor, kwargs))


@pytest.mark.parametrize(
    "submission_mode",
    ["NEW_TASK", "SUPPLEMENT_TASK"],
)
def test_non_replayed_ready_route_invokes_complete_tool_workflow(
    submission_mode: str,
) -> None:
    submission, projection = _ready_route_projection(
        submission_mode=submission_mode,
    )
    actor = ActorContext(actor_id="actor_route", user_id=None)

    with pytest.raises(_WorkflowInvoked) as raised:
        submit_message(
            conversation_id="conversation_route",
            body=MessageSubmissionRequest(
                submission_mode=submission_mode,
                content_text="complete tool input",
                target_task_id=(
                    "task_route"
                    if submission_mode == "SUPPLEMENT_TASK"
                    else None
                ),
            ),
            request=SimpleNamespace(
                state=SimpleNamespace(request_id="request_route")
            ),
            actor_context=actor,
            service=_DirectSubmissionService(submission),
            orchestration_service=_DirectOrchestrationService(projection),
            tool_workflow_service=_DirectWorkflowService(),
            idempotency_key="message-route-key",
        )

    assert raised.value.args[0] == (
        actor,
        {
            "task_id": "task_route",
            "task_input_revision_id": "revision_route",
            "request_id": "request_route",
        },
    )


def test_ready_route_without_workflow_stays_ready_without_fake_result() -> None:
    submission, projection = _ready_route_projection(
        submission_mode="NEW_TASK",
    )

    response = submit_message(
        conversation_id="conversation_route",
        body=MessageSubmissionRequest(
            submission_mode="NEW_TASK",
            content_text="complete tool input",
        ),
        request=SimpleNamespace(
            state=SimpleNamespace(request_id="request_route")
        ),
        actor_context=ActorContext(actor_id="actor_route", user_id=None),
        service=_DirectSubmissionService(submission),
        orchestration_service=_DirectOrchestrationService(projection),
        tool_workflow_service=None,
        idempotency_key="message-route-key",
    )

    assert response.data.task.status == "READY"
    assert response.data.result_summary is None
    assert response.data.explanation is None


@pytest.mark.parametrize("workflow_available", [True, False])
def test_ready_route_rejects_missing_revision(
    workflow_available: bool,
) -> None:
    submission, projection = _ready_route_projection(
        submission_mode="NEW_TASK",
        include_revision=False,
    )

    with pytest.raises(ApplicationInternalError):
        submit_message(
            conversation_id="conversation_route",
            body=MessageSubmissionRequest(
                submission_mode="NEW_TASK",
                content_text="complete tool input",
            ),
            request=SimpleNamespace(
                state=SimpleNamespace(request_id="request_route")
            ),
            actor_context=ActorContext(
                actor_id="actor_route",
                user_id=None,
            ),
            service=_DirectSubmissionService(submission),
            orchestration_service=_DirectOrchestrationService(projection),
            tool_workflow_service=(
                _DirectWorkflowService() if workflow_available else None
            ),
            idempotency_key="message-route-key",
        )


def _create_conversation(client) -> str:
    response = client.post("/api/v1/conversations", json={})
    assert response.status_code == 201
    return response.json()["data"]["conversation_id"]


def _submit(client, conversation_id: str, content_text: str):
    return client.post(
        f"/api/v1/conversations/{conversation_id}/messages",
        headers={"Idempotency-Key": f"message-{uuid4().hex}"},
        json={
            "submission_mode": "NEW_TASK",
            "content_text": content_text,
        },
    )


def _supplement(client, conversation_id: str, task_id: str, content_text: str):
    return client.post(
        f"/api/v1/conversations/{conversation_id}/messages",
        headers={"Idempotency-Key": f"message-{uuid4().hex}"},
        json={
            "submission_mode": "SUPPLEMENT_TASK",
            "target_task_id": task_id,
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


def test_bound_missing_input_is_supplemented_without_first_route_rebinding(
    api_harness,
) -> None:
    actor_id = "actor_local"
    api_harness.persist_actor(actor_id)

    with api_harness.create_client(actor_id) as client:
        conversation_id = _create_conversation(client)
        first = _submit(client, conversation_id, "缺 aging_temperature")
        first_data = first.json()["data"]
        task_id = first_data["task"]["task_id"]
        supplement = _supplement(
            client,
            conversation_id,
            task_id,
            "时效温度 730 °C",
        )

    assert first.status_code == 200
    assert supplement.status_code == 200
    assert supplement.json()["data"]["task"]["status"] == "READY"
    messages, tasks, revisions, calls = _rows(api_harness)
    assert len(revisions) == 2
    assert revisions[0].revision == 1
    assert revisions[1].revision == 2
    assert revisions[1].raw_input == {
        "aging_temperature": {"value": 730, "unit": "°C"}
    }
    assert revisions[1].normalized_input["solution_temperature"] == {
        "value": 1000,
        "unit": "°C",
    }
    assert tasks[0].tool_id == "zta35g_sem_virtual_lab"
    calls_by_purpose = {call.purpose: call for call in calls}
    assert set(calls_by_purpose) == {
        "CHAT_ORCHESTRATION",
        "TOOL_INPUT_EXTRACTION",
    }
    extraction_call = calls_by_purpose["TOOL_INPUT_EXTRACTION"]
    assert extraction_call.catalog_snapshot_refs is None
    assert extraction_call.catalog_hash is None
    assert extraction_call.tool_context_ref == {
        "tool_id": "zta35g_sem_virtual_lab",
        "version": tasks[0].bound_tool_version,
        "schema_hash": tasks[0].bound_schema_hash,
    }


def test_hard_invalid_message_returns_agent_internal_without_binding(
    api_harness,
) -> None:
    actor_id = "actor_local"
    api_harness.persist_actor(actor_id)

    with api_harness.create_client(actor_id) as client:
        conversation_id = _create_conversation(client)
        response = _submit(client, conversation_id, "越界温度")

    assert response.status_code == 500
    body = response.json()
    assert body["error"]["code"] == "AGENT_INTERNAL_ERROR"
    assert body["resource"]["conversation_id"] == conversation_id
    assert body["resource"]["task_id"]
    messages, tasks, revisions, calls = _rows(api_harness)
    assert len(messages) == 1
    assert len(revisions) == 0
    assert calls[0].status == "FAILED"
    assert calls[0].error_code == "LLM_SCHEMA_MISMATCH"
    assert tasks[0].current_status == "FAILED"
    assert tasks[0].error_code == "AGENT_INTERNAL_ERROR"
    assert tasks[0].tool_id is None


def test_complete_valid_tool_message_returns_bound_ready_without_fake_result(
    api_harness,
) -> None:
    actor_id = "actor_local"
    api_harness.persist_actor(actor_id)

    with api_harness.create_client(actor_id) as client:
        conversation_id = _create_conversation(client)
        response = _submit(client, conversation_id, "完整合法 Tool 请求")
        task_response = client.get(f"/api/v1/tasks/{response.json()['data']['task']['task_id']}")

    assert response.status_code == 200
    body = response.json()
    assert body["data"]["conversation_id"] == conversation_id
    assert body["data"]["task"]["status"] == "READY"
    assert task_response.status_code == 200
    task_data = task_response.json()["data"]
    assert task_data["task_type"] == "TOOL_EXECUTION"
    assert task_data["status"] == "READY"
    assert task_data["error_code"] is None
    assert task_data["selected_tool_run_id"] is None
    assert task_data["selected_result_id"] is None
    messages, tasks, revisions, calls = _rows(api_harness)
    assert len(messages) == 1
    assert len(revisions) == 1
    assert calls[0].status == "SUCCEEDED"
    assert tasks[0].tool_id == "zta35g_sem_virtual_lab"
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

    assert response.status_code == 200
    body = response.json()
    assert body["data"]["task"]["status"] == "READY"
    assert storage.calls == 0
    assert storage.put_calls == 0
    assert storage.objects == {}
    messages, tasks, revisions, calls = _rows(api_harness)
    assert len(messages) == 1
    assert len(tasks) == 1
    assert len(revisions) == 1
    assert len(calls) == 1
    assert tasks[0].current_status == "READY"
    assert tasks[0].error_code is None
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

    assert response.status_code == 200
    assert response.json()["data"]["task"]["status"] == "READY"
    with api_harness.engine.connect() as connection:
        assert connection.scalar(text("SELECT count(*) FROM tool_run")) == 0
        assert connection.scalar(text("SELECT count(*) FROM asset")) == 0
        assert connection.scalar(text("SELECT count(*) FROM tool_result")) == 0
        assert connection.scalar(
            text("SELECT count(*) FROM natural_language_explanation")
        ) == 0


def test_solution_time_minutes_are_visible_only_as_committed_ready_revision(
    api_harness,
) -> None:
    actor_id = "actor_local"
    api_harness.persist_actor(actor_id)

    with api_harness.create_client(actor_id) as client:
        conversation_id = _create_conversation(client)
        response = _submit(client, conversation_id, "solution_time = 180 min")

    assert response.status_code == 200
    _, _, revisions, _ = _rows(api_harness)
    assert revisions[0].raw_input["solution_time"] == {
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
        ("Mock protocol failure", 500, "AGENT_INTERNAL_ERROR"),
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
