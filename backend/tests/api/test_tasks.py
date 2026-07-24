from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone

from materialsagent.application.errors import (
    ApplicationInternalError,
)
from materialsagent.application.tasks import TaskQueryService
from materialsagent.domain.ports.timeline_query import (
    TaskDetailQuerySnapshot,
)
from materialsagent.domain.ports.unit_of_work import (
    DatabaseUnavailableError,
    PersistenceError,
)


BASE_TIME = datetime(2026, 7, 19, 0, 0, tzinfo=timezone.utc)


def _assert_utc(value: str | None) -> None:
    assert value is not None
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    assert parsed.tzinfo is not None
    assert parsed.utcoffset() == timezone.utc.utcoffset(parsed)
    assert value.endswith("Z")


def _task_snapshot(row) -> tuple[object, ...]:
    return (
        row.task_id,
        row.conversation_id,
        row.actor_id,
        row.task_type,
        row.current_status,
        row.selected_tool_run_id,
        row.selected_result_id,
        row.created_at,
        row.started_at,
        row.updated_at,
        row.completed_at,
        row.error_code,
        row.safe_error_message,
    )


def test_get_owned_pending_task_returns_exact_public_projection_without_writes(
    api_harness,
) -> None:
    actor_id = "actor_local"
    api_harness.persist_actor(actor_id)
    conversation = api_harness.persist_conversation(actor_id)
    _, task = api_harness.persist_message_task(
        conversation,
        content_text="initial",
        created_at=BASE_TIME + timedelta(minutes=1),
    )
    before_counts = api_harness.counts()
    before_task = _task_snapshot(api_harness.task_rows()[0])

    with api_harness.create_client(actor_id) as client:
        first = client.get(f"/api/v1/tasks/{task.task_id}")
        second = client.get(f"/api/v1/tasks/{task.task_id}")

    assert first.status_code == second.status_code == 200
    assert first.json()["request_id"] != second.json()["request_id"]
    body = first.json()
    assert set(body) == {"request_id", "data"}
    assert set(body["data"]) == {
        "task_id",
        "conversation_id",
        "task_type",
        "status",
        "anchor_at",
        "selected_tool_run_id",
        "selected_result_id",
        "created_at",
        "started_at",
        "updated_at",
        "completed_at",
        "error_code",
        "safe_error_message",
        "needs_input",
        "tool_run_count",
        "tool_runs",
        "selected_result_summary",
        "assets",
        "explanation_summary",
        "latest_explanation_failure",
    }
    assert body["data"]["task_id"] == task.task_id
    assert body["data"]["conversation_id"] == conversation.conversation_id
    assert body["data"]["task_type"] is None
    assert body["data"]["status"] == "PENDING"
    assert body["data"]["selected_tool_run_id"] is None
    assert body["data"]["selected_result_id"] is None
    assert body["data"]["started_at"] is None
    assert body["data"]["completed_at"] is None
    assert body["data"]["error_code"] is None
    assert body["data"]["safe_error_message"] is None
    assert body["data"]["selected_result_summary"] is None
    assert body["data"]["anchor_at"] == body["data"]["created_at"]
    assert body["data"]["needs_input"] is None
    assert body["data"]["tool_run_count"] == 0
    assert body["data"]["tool_runs"] == []
    assert body["data"]["assets"] == []
    assert body["data"]["explanation_summary"] is None
    assert body["data"]["latest_explanation_failure"] is None
    _assert_utc(body["data"]["created_at"])
    _assert_utc(body["data"]["updated_at"])
    assert "actor_id" not in first.text
    assert api_harness.counts() == before_counts
    assert _task_snapshot(api_harness.task_rows()[0]) == before_task


def test_foreign_and_missing_task_have_identical_safe_404_even_with_actor_header(
    api_harness,
) -> None:
    actor_id = "actor_local"
    foreign_actor_id = "actor_foreign"
    api_harness.persist_actor(actor_id)
    api_harness.persist_actor(foreign_actor_id)
    foreign_conversation = api_harness.persist_conversation(foreign_actor_id)
    _, foreign_task = api_harness.persist_message_task(
        foreign_conversation,
        content_text="foreign",
        created_at=BASE_TIME + timedelta(minutes=1),
    )
    before_counts = api_harness.counts()

    with api_harness.create_client(actor_id) as client:
        foreign = client.get(
            f"/api/v1/tasks/{foreign_task.task_id}",
            headers={"X-Actor-Id": foreign_actor_id},
        )
        missing = client.get("/api/v1/tasks/task_missing")

    for response in (foreign, missing):
        assert response.status_code == 404
        body = response.json()
        assert body["request_id"].startswith("req_")
        assert body["error"] == {
            "code": "RESOURCE_NOT_FOUND",
            "message": "请求的资源不存在。",
            "details": [],
        }
        assert set(body["resource"]) == {
            "conversation_id",
            "task_id",
            "tool_run_id",
            "result_id",
        }
        assert "actor_id" not in response.text
        assert "Traceback" not in response.text
        assert "SELECT" not in response.text
        assert "C:\\Users" not in response.text
    assert foreign.json()["error"] == missing.json()["error"]
    assert api_harness.counts() == before_counts


def test_get_task_returns_persisted_tool_unavailable_terminal_fact(
    api_harness,
) -> None:
    actor_id = "actor_local"
    api_harness.persist_actor(actor_id)

    with api_harness.create_client(actor_id) as client:
        conversation = client.post("/api/v1/conversations", json={})
        conversation_id = conversation.json()["data"]["conversation_id"]
        submitted = client.post(
            f"/api/v1/conversations/{conversation_id}/messages",
            headers={"Idempotency-Key": "task-query-source"},
            json={
                "submission_mode": "NEW_TASK",
                "content_text": "完整合法 Tool 请求",
            },
        )
        assert submitted.status_code == 503
        task_id = submitted.json()["resource"]["task_id"]
        response = client.get(f"/api/v1/tasks/{task_id}")

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["task_id"] == task_id
    assert data["conversation_id"] == conversation_id
    assert data["task_type"] == "TOOL_EXECUTION"
    assert data["status"] == "FAILED"
    assert data["error_code"] == "TOOL_UNAVAILABLE"
    assert data["safe_error_message"] == "当前阶段尚未开放材料工具执行。"
    assert data["selected_tool_run_id"] is None
    assert data["selected_result_id"] is None
    assert data["selected_result_summary"] is None
    assert data["anchor_at"] is not None
    assert data["tool_run_count"] == 0
    assert data["tool_runs"] == []


def test_get_task_returns_safe_full_tool_history_without_external_calls(
    api_harness,
) -> None:
    from backend.tests.api.test_assets import _MemoryStorage
    from backend.tests.api.test_explanation_outcomes import (
        _Runtime,
        _client_options,
        _submit,
    )

    actor_id = "actor_local"
    api_harness.persist_actor(actor_id)
    runtime = _Runtime()
    storage = _MemoryStorage()

    with api_harness.create_client(
        actor_id,
        **_client_options(
            api_harness,
            runtime,
            storage,
            "success",
        ),
        raise_server_exceptions=True,
    ) as client:
        conversation_id = client.post(
            "/api/v1/conversations",
            json={},
        ).json()["data"]["conversation_id"]
        submitted = _submit(client, conversation_id)
        assert submitted.status_code == 200
        task_id = submitted.json()["data"]["task"]["task_id"]
        runtime_calls_before_get = runtime.calls
        response = client.get(f"/api/v1/tasks/{task_id}")

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["anchor_at"] == (
        submitted.json()["data"]["user_message"]["created_at"]
    )
    assert data["tool_run_count"] == 1
    assert len(data["tool_runs"]) == 1
    run = data["tool_runs"][0]
    assert run["tool_run_id"] == data["selected_tool_run_id"]
    assert run["attempt_no"] == 1
    assert run["is_selected"] is True
    assert data["selected_result_summary"]["result_id"] == (
        data["selected_result_id"]
    )
    assert len(data["assets"]) == 1
    assert data["assets"][0]["content_url"].startswith("/api/v1/assets/")
    assert data["explanation_summary"]["status"] == "SUCCEEDED"
    assert data["latest_explanation_failure"] is None
    assert runtime.calls == runtime_calls_before_get
    assert "execution_input" not in response.text
    assert "actual_runtime_parameters" not in response.text
    assert "model_bundle" not in response.text
    assert "object_key" not in response.text
    public_text = response.text.lower()
    for forbidden in (
        "bucket",
        "runtime_url",
        "token",
        "weight_path",
        "prompt",
        "traceback",
        ":\\",
    ):
        assert forbidden not in public_text
    assert data["started_at"] is not None
    assert data["completed_at"] is not None


class _FailingTaskDetailQuery:
    def __init__(self, error: PersistenceError) -> None:
        self._error = error

    def fetch_owned_task_detail(self, **_kwargs):
        raise self._error


class _CorruptingTaskDetailQuery:
    def __init__(self, query) -> None:
        self._query = query

    def fetch_owned_task_detail(
        self,
        *,
        actor_id: str,
        task_id: str,
    ) -> TaskDetailQuerySnapshot | None:
        snapshot = self._query.fetch_owned_task_detail(
            actor_id=actor_id,
            task_id=task_id,
        )
        if snapshot is None:
            return None
        corrupted_task = replace(
            snapshot.task,
            selected_tool_run_id="run_missing",
            selected_result_id="result_missing",
        )
        return replace(snapshot, task=corrupted_task)


def test_task_get_maps_database_unavailable_and_persistence_failure_safely(
    api_harness,
) -> None:
    actor_id = "actor_local"
    api_harness.persist_actor(actor_id)
    conversation = api_harness.persist_conversation(actor_id)
    _, task = api_harness.persist_message_task(
        conversation,
        content_text="task",
        created_at=BASE_TIME + timedelta(minutes=1),
    )

    cases = (
        (
            DatabaseUnavailableError("private database detail"),
            503,
            "DEPENDENCY_UNAVAILABLE",
        ),
        (
            PersistenceError("private persistence detail"),
            500,
            "INTERNAL_ERROR",
        ),
    )
    for error, status_code, code in cases:
        with api_harness.create_client(
            actor_id,
            task_query_service=TaskQueryService(
                _FailingTaskDetailQuery(error)
            ),
        ) as client:
            response = client.get(f"/api/v1/tasks/{task.task_id}")

        assert response.status_code == status_code
        assert response.json()["error"]["code"] == code
        assert "private" not in response.text
        assert "Traceback" not in response.text


def test_task_get_selected_source_corruption_is_safe_500(
    api_harness,
) -> None:
    from materialsagent.infrastructure.db.timeline_query import (
        SQLAlchemyTimelineQueryRepository,
    )

    actor_id = "actor_local"
    api_harness.persist_actor(actor_id)
    conversation = api_harness.persist_conversation(actor_id)
    _, task = api_harness.persist_message_task(
        conversation,
        content_text="task",
        created_at=BASE_TIME + timedelta(minutes=1),
    )
    service = TaskQueryService(
        _CorruptingTaskDetailQuery(
            SQLAlchemyTimelineQueryRepository(api_harness.engine)
        )
    )

    with api_harness.create_client(
        actor_id,
        task_query_service=service,
    ) as client:
        response = client.get(f"/api/v1/tasks/{task.task_id}")

    assert response.status_code == 500
    assert response.json()["error"] == {
        "code": "INTERNAL_ERROR",
        "message": ApplicationInternalError.default_message,
        "details": [],
    }
    assert "run_missing" not in response.text
    assert "result_missing" not in response.text


def test_missing_timeline_signing_key_does_not_disable_production_task_query(
    api_harness,
) -> None:
    from fastapi.testclient import TestClient

    from materialsagent.application.context import ActorContext
    from materialsagent.application.readiness import ReadinessService
    from materialsagent.main import create_app

    actor_id = "actor_local"
    api_harness.persist_actor(actor_id)
    conversation = api_harness.persist_conversation(actor_id)
    _, task = api_harness.persist_message_task(
        conversation,
        content_text="task",
        created_at=BASE_TIME + timedelta(minutes=1),
    )
    settings = api_harness.settings.model_copy(
        update={"timeline_cursor_signing_key": None}
    )

    with TestClient(
        create_app(
            settings=settings,
            readiness_service=ReadinessService(
                postgresql_probe=lambda: True,
                object_storage_probe=lambda: True,
            ),
            actor_context=ActorContext(
                actor_id=actor_id,
                user_id=None,
            ),
            m7_tool_chain_enabled=False,
        )
    ) as client:
        task_response = client.get(f"/api/v1/tasks/{task.task_id}")
        timeline_response = client.get(
            f"/api/v1/conversations/{conversation.conversation_id}/timeline"
        )

    assert task_response.status_code == 200
    assert task_response.json()["data"]["task_id"] == task.task_id
    assert timeline_response.status_code == 503
    assert timeline_response.json()["error"]["code"] == (
        "DEPENDENCY_UNAVAILABLE"
    )
