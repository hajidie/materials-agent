from __future__ import annotations

from datetime import datetime, timedelta, timezone


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
        "selected_tool_run_id",
        "selected_result_id",
        "created_at",
        "started_at",
        "updated_at",
        "completed_at",
        "error_code",
        "safe_error_message",
        "selected_result_summary",
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
    assert data["started_at"] is not None
    assert data["completed_at"] is not None
