from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone

from pydantic import SecretStr
from sqlalchemy import update

from materialsagent.application.timeline import TimelineQueryService
from materialsagent.application.timeline_cursor import TimelineCursorCodec
from materialsagent.domain.ports.unit_of_work import (
    DatabaseUnavailableError,
    PersistenceError,
)
from materialsagent.domain.models.message import Message
from materialsagent.infrastructure.db.conversation_task import (
    MessageRow,
    TaskInputRevisionRow,
    TaskRow,
)


BASE = datetime(2026, 7, 24, 5, 0, tzinfo=timezone.utc)


def _assistant(
    message_id: str,
    *,
    conversation_id: str,
    task_id: str,
    actor_id: str,
    created_at: datetime,
) -> Message:
    return Message(
        message_id=message_id,
        conversation_id=conversation_id,
        task_id=task_id,
        actor_id=actor_id,
        request_id=f"request_{message_id}",
        role="ASSISTANT",
        generation_source="TEMPLATE",
        content_text=message_id,
        structured_content=None,
        llm_call_id=None,
        created_at=created_at,
    )


def _seed_mixed(api_harness, actor_id: str = "actor_local"):
    api_harness.persist_actor(actor_id)
    conversation = api_harness.persist_conversation(
        actor_id,
        conversation_id="conversation_timeline",
        title="统一时间线",
        created_at=BASE,
        updated_at=BASE,
    )
    knowledge_message, knowledge_task = api_harness.persist_message_task(
        conversation,
        content_text="知识问题",
        created_at=BASE,
        message_id="message_a",
        task_id="task_knowledge",
    )
    tool_message, tool_task = api_harness.persist_message_task(
        conversation,
        content_text="工具请求",
        created_at=BASE,
        message_id="message_tool_initial",
        task_id="task_tool",
    )
    with api_harness.unit_of_work_factory() as unit_of_work:
        unit_of_work.messages.add(
            _assistant(
                "message_b",
                conversation_id=conversation.conversation_id,
                task_id=knowledge_task.task_id,
                actor_id=actor_id,
                created_at=BASE,
            )
        )
        unit_of_work.messages.add(
            _assistant(
                "message_tool_follow_up",
                conversation_id=conversation.conversation_id,
                task_id=tool_task.task_id,
                actor_id=actor_id,
                created_at=BASE + timedelta(seconds=1),
            )
        )
        unit_of_work.commit()
    with api_harness.engine.begin() as connection:
        connection.execute(
            update(TaskRow)
            .where(TaskRow.task_id == knowledge_task.task_id)
            .values(task_type="KNOWLEDGE_QA")
        )
        connection.execute(
            update(TaskRow)
            .where(TaskRow.task_id == tool_task.task_id)
            .values(task_type="TOOL_EXECUTION")
        )
    return conversation, knowledge_message, tool_message


def _seed_knowledge_prefix(api_harness, actor_id: str):
    prefix_time = datetime(2026, 7, 19, 0, 0, tzinfo=timezone.utc)
    api_harness.persist_actor(actor_id)
    conversation = api_harness.persist_conversation(
        actor_id,
        created_at=prefix_time,
        updated_at=prefix_time,
    )
    _, task = api_harness.persist_message_task(
        conversation,
        content_text="知识问题",
        created_at=prefix_time,
        message_id="message_prefix_user",
        task_id="task_prefix",
    )
    with api_harness.unit_of_work_factory() as unit_of_work:
        unit_of_work.messages.add(
            _assistant(
                "message_prefix_assistant",
                conversation_id=conversation.conversation_id,
                task_id=task.task_id,
                actor_id=actor_id,
                created_at=prefix_time,
            )
        )
        unit_of_work.commit()
    with api_harness.engine.begin() as connection:
        connection.execute(
            update(TaskRow)
            .where(TaskRow.task_id == task.task_id)
            .values(task_type="KNOWLEDGE_QA")
        )
    return conversation


def test_timeline_returns_discriminated_mixed_items_without_tool_duplicate(
    api_harness,
) -> None:
    conversation, _, _ = _seed_mixed(api_harness)

    with api_harness.create_client("actor_local") as client:
        response = client.get(
            f"/api/v1/conversations/{conversation.conversation_id}/timeline"
        )

    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"request_id", "data"}
    assert body["data"]["conversation"] == {
        "conversation_id": conversation.conversation_id,
        "title": "统一时间线",
    }
    assert body["data"]["has_more"] is False
    assert body["data"]["next_cursor"] is None
    items = body["data"]["items"]
    assert [
        (item["item_type"], item["item_id"]) for item in items
    ] == [
        ("USER_MESSAGE", "message_a"),
        ("ASSISTANT_MESSAGE", "message_b"),
        ("TOOL_TASK", "task_tool"),
    ]
    tool = items[-1]
    assert tool["initial_user_message"]["message_id"] == (
        "message_tool_initial"
    )
    assert [
        item["message_id"] for item in tool["input_thread"]
    ] == ["message_tool_follow_up"]
    assert tool["input_thread_count"] == 1
    assert tool["input_thread_truncated"] is False
    assert tool["tool_runs"] == {
        "attempt_count": 0,
        "selected_tool_run": None,
        "has_history": False,
    }
    assert tool["result"] is None
    assert tool["assets"] == []
    assert tool["explanation"] is None
    assert tool["latest_explanation_failure"] is None
    assert tool["needs_input"] is None
    assert "message_tool_initial" not in [
        item["item_id"] for item in items
    ]
    assert "actor_id" not in response.text
    assert "object_key" not in response.text
    assert "model_bundle" not in response.text


def test_timeline_projects_unbound_candidate_references_without_binding(
    api_harness,
) -> None:
    conversation, _, _ = _seed_mixed(api_harness)
    candidate_refs = [
        {"tool_id": "zta35g_sem_virtual_lab", "version": "1", "schema_hash": "a" * 64},
        {"tool_id": "training", "version": "1", "schema_hash": "b" * 64},
    ]
    with api_harness.engine.begin() as connection:
        connection.execute(
            TaskInputRevisionRow.__table__.insert().values(
                task_input_revision_id="revision_timeline_candidates",
                task_id="task_tool",
                request_id="request_timeline_candidates",
                source_llm_call_id=None,
                source_message_ids=["message_tool_initial"],
                revision=1,
                raw_input={},
                normalized_input=None,
                missing_fields=[],
                ambiguous_fields=[],
                validation_errors=[],
                candidate_tool_refs=candidate_refs,
                created_at=BASE,
            )
        )
        connection.execute(
            update(TaskRow)
            .where(TaskRow.task_id == "task_tool")
            .values(
                current_status="NEEDS_INPUT",
                tool_id=None,
                bound_tool_version=None,
                bound_schema_hash=None,
            )
        )

    with api_harness.create_client("actor_local") as client:
        response = client.get(
            f"/api/v1/conversations/{conversation.conversation_id}/timeline"
        )

    assert response.status_code == 200
    card = next(
        item
        for item in response.json()["data"]["items"]
        if item["item_id"] == "task_tool"
    )
    assert card["task"]["tool_id"] is None
    assert card["task"]["bound_tool_version"] is None
    assert card["task"]["bound_schema_hash"] is None
    assert card["needs_input"]["candidate_tool_refs"] == candidate_refs


def test_timeline_keyset_cursor_is_stable_replayable_and_tamper_safe(
    api_harness,
) -> None:
    conversation, _, _ = _seed_mixed(api_harness)

    with api_harness.create_client("actor_local") as client:
        first = client.get(
            f"/api/v1/conversations/{conversation.conversation_id}/timeline",
            params={"limit": 2},
        )
        cursor = first.json()["data"]["next_cursor"]
        second = client.get(
            f"/api/v1/conversations/{conversation.conversation_id}/timeline",
            params={"limit": 2, "cursor": cursor},
        )
        replay = client.get(
            f"/api/v1/conversations/{conversation.conversation_id}/timeline",
            params={"limit": 2, "cursor": cursor},
        )
        tampered = client.get(
            f"/api/v1/conversations/{conversation.conversation_id}/timeline",
            params={"cursor": cursor + "x"},
        )

    assert first.status_code == second.status_code == replay.status_code == 200
    assert first.json()["data"]["has_more"] is True
    assert cursor
    assert [
        item["item_id"] for item in second.json()["data"]["items"]
    ] == ["task_tool"]
    assert replay.json()["data"] == second.json()["data"]
    assert tampered.status_code == 422
    assert tampered.json()["error"] == {
        "code": "VALIDATION_FAILED",
        "message": "分页游标无效。",
        "details": [],
    }


def test_timeline_cross_conversation_cursor_and_limit_are_rejected(
    api_harness,
) -> None:
    conversation, _, _ = _seed_mixed(api_harness)
    other = api_harness.persist_conversation(
        "actor_local",
        conversation_id="conversation_other",
        created_at=BASE,
        updated_at=BASE,
    )

    with api_harness.create_client("actor_local") as client:
        first = client.get(
            f"/api/v1/conversations/{conversation.conversation_id}/timeline",
            params={"limit": 1},
        )
        cursor = first.json()["data"]["next_cursor"]
        cross = client.get(
            f"/api/v1/conversations/{other.conversation_id}/timeline",
            params={"cursor": cursor},
        )
        low = client.get(
            f"/api/v1/conversations/{conversation.conversation_id}/timeline",
            params={"limit": 0},
        )
        high = client.get(
            f"/api/v1/conversations/{conversation.conversation_id}/timeline",
            params={"limit": 51},
        )

    assert cross.status_code == 422
    assert cross.json()["error"]["message"] == "分页游标无效。"
    assert low.status_code == high.status_code == 422


def test_timeline_foreign_and_missing_conversation_have_same_safe_404(
    api_harness,
) -> None:
    api_harness.persist_actor("actor_local")
    api_harness.persist_actor("actor_foreign")
    foreign = api_harness.persist_conversation(
        "actor_foreign",
        conversation_id="conversation_foreign",
    )

    with api_harness.create_client("actor_local") as client:
        foreign_response = client.get(
            f"/api/v1/conversations/{foreign.conversation_id}/timeline"
        )
        missing_response = client.get(
            "/api/v1/conversations/conversation_missing/timeline"
        )

    for response in (foreign_response, missing_response):
        assert response.status_code == 404
        assert response.json()["error"] == {
            "code": "RESOURCE_NOT_FOUND",
            "message": "请求的资源不存在。",
            "details": [],
        }
    assert foreign_response.json()["error"] == (
        missing_response.json()["error"]
    )


def test_missing_timeline_key_only_disables_timeline_endpoint(
    api_harness,
) -> None:
    api_harness.persist_actor("actor_local")
    conversation = api_harness.persist_conversation("actor_local")

    with api_harness.create_client(
        "actor_local",
        configure_timeline=False,
    ) as client:
        live = client.get("/api/v1/health/live")
        existing = client.get(f"/api/v1/tasks/task_missing")
        timeline = client.get(
            f"/api/v1/conversations/{conversation.conversation_id}/timeline"
        )

    assert live.status_code == 200
    assert existing.status_code == 404
    assert timeline.status_code == 503
    assert timeline.json()["error"]["code"] == "DEPENDENCY_UNAVAILABLE"


def test_timeline_data_corruption_maps_to_safe_500(
    api_harness,
) -> None:
    conversation, knowledge_message, _ = _seed_mixed(api_harness)
    api_harness.persist_actor("actor_corrupt")
    with api_harness.engine.begin() as connection:
        connection.execute(
            update(MessageRow)
            .where(MessageRow.message_id == knowledge_message.message_id)
            .values(actor_id="actor_corrupt")
        )

    with api_harness.create_client("actor_local") as client:
        response = client.get(
            f"/api/v1/conversations/{conversation.conversation_id}/timeline"
        )

    assert response.status_code == 500
    assert response.json()["error"] == {
        "code": "INTERNAL_ERROR",
        "message": "内部处理失败。",
        "details": [],
    }
    assert "actor_corrupt" not in response.text
    assert "Traceback" not in response.text
    assert "SELECT" not in response.text


class _UnavailableTimelineQuery:
    def fetch_owned_page(self, **_kwargs):
        raise DatabaseUnavailableError("private database detail")


class _BrokenTimelineQuery:
    def fetch_owned_page(self, **_kwargs):
        raise PersistenceError("private persistence detail")


def test_timeline_database_unavailable_maps_to_safe_503(
    api_harness,
) -> None:
    api_harness.persist_actor("actor_local")
    conversation = api_harness.persist_conversation("actor_local")
    service = TimelineQueryService(
        _UnavailableTimelineQuery(),
        TimelineCursorCodec(
            SecretStr(
                "api-test-timeline-signing-key-at-least-32-bytes"
            )
        ),
    )

    with api_harness.create_client(
        "actor_local",
        timeline_query_service=service,
    ) as client:
        response = client.get(
            f"/api/v1/conversations/{conversation.conversation_id}/timeline"
        )

    assert response.status_code == 503
    assert response.json()["error"] == {
        "code": "DEPENDENCY_UNAVAILABLE",
        "message": "依赖服务暂不可用。",
        "details": [],
    }
    assert "private database detail" not in response.text


def test_timeline_generic_persistence_failure_maps_to_safe_500(
    api_harness,
) -> None:
    api_harness.persist_actor("actor_local")
    conversation = api_harness.persist_conversation("actor_local")
    service = TimelineQueryService(
        _BrokenTimelineQuery(),
        TimelineCursorCodec(
            SecretStr(
                "api-test-timeline-signing-key-at-least-32-bytes"
            )
        ),
    )

    with api_harness.create_client(
        "actor_local",
        timeline_query_service=service,
    ) as client:
        response = client.get(
            f"/api/v1/conversations/{conversation.conversation_id}/timeline"
        )

    assert response.status_code == 500
    assert response.json()["error"] == {
        "code": "INTERNAL_ERROR",
        "message": "内部处理失败。",
        "details": [],
    }
    assert "private persistence detail" not in response.text


def test_timeline_projects_full_selected_chain_without_external_calls(
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
            headers={"Idempotency-Key": "timeline-conversation"},
            json={},
        ).json()["data"]["conversation_id"]
        submitted = _submit(client, conversation_id)
        assert submitted.status_code == 200
        runtime_calls_before_get = runtime.calls
        response = client.get(
            f"/api/v1/conversations/{conversation_id}/timeline"
        )

    assert response.status_code == 200
    items = response.json()["data"]["items"]
    assert len(items) == 1
    card = items[0]
    assert card["item_type"] == "TOOL_TASK"
    assert card["initial_user_message"]["message_id"] == (
        submitted.json()["data"]["user_message"]["message_id"]
    )
    assert card["tool_runs"]["attempt_count"] == 1
    assert card["tool_runs"]["selected_tool_run"]["is_selected"] is True
    assert card["tool_runs"]["selected_tool_run"]["schema_hash"] == (
        "f821240f782ce788bc723fd1acd02a2e58cedbf68b70b1414e2accd16d989d07"
    )
    assert card["result"]["result_id"] == (
        submitted.json()["data"]["result_summary"]["result_id"]
    )
    assert card["result"]["schema_hash"] == (
        "f821240f782ce788bc723fd1acd02a2e58cedbf68b70b1414e2accd16d989d07"
    )
    assert len(card["assets"]) == 1
    assert card["explanation"]["status"] == "SUCCEEDED"
    assert card["latest_explanation_failure"] is None
    assert runtime.calls == runtime_calls_before_get
    assert "execution_input" not in response.text
    assert "private_path" not in response.text
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


def test_oversized_cursor_uses_the_fixed_invalid_cursor_error(
    api_harness,
) -> None:
    api_harness.persist_actor("actor_local")
    conversation = api_harness.persist_conversation("actor_local")

    with api_harness.create_client("actor_local") as client:
        response = client.get(
            f"/api/v1/conversations/{conversation.conversation_id}/timeline",
            params={"cursor": "a" * 2049},
        )

    assert response.status_code == 422
    assert response.json()["error"]["message"] == "分页游标无效。"


def test_old_cursor_keeps_tool_card_position_after_tool_retry(
    api_harness,
) -> None:
    from backend.tests.api.test_assets import _MemoryStorage
    from backend.tests.api.test_explanation_outcomes import _Runtime
    from backend.tests.api.test_m8_tool_retry import (
        _Seeds,
        _client_options,
    )
    from materialsagent.infrastructure.llm.mock_explanation import (
        MockExplanationAdapter,
    )

    actor_id = "actor_timeline_tool_retry"
    conversation = _seed_knowledge_prefix(api_harness, actor_id)
    runtime = _Runtime(partial=True)
    storage = _MemoryStorage()
    explanation = MockExplanationAdapter()
    with api_harness.create_client(
        actor_id,
        **_client_options(
            api_harness,
            runtime,
            storage,
            explanation,
            _Seeds(101, 202),
        ),
    ) as client:
        submitted = client.post(
            f"/api/v1/conversations/{conversation.conversation_id}/messages",
            headers={"Idempotency-Key": "timeline-initial-tool"},
            json={
                "submission_mode": "NEW_TASK",
                "content_text": "完整合法 Tool 请求",
            },
        )
        assert submitted.status_code == 200
        task = submitted.json()["data"]["task"]
        first = client.get(
            f"/api/v1/conversations/{conversation.conversation_id}/timeline",
            params={"limit": 2},
        )
        cursor = first.json()["data"]["next_cursor"]
        before = client.get(
            f"/api/v1/conversations/{conversation.conversation_id}/timeline",
            params={"cursor": cursor},
        ).json()["data"]["items"][0]
        assert runtime.calls == 1
        runtime.partial = False
        retried = client.post(
            f"/api/v1/tasks/{task['task_id']}/tool-runs",
            headers={"Idempotency-Key": "timeline-tool-retry"},
            json={},
        )
        assert retried.status_code == 200
        assert runtime.calls == 2
        after_response = client.get(
            f"/api/v1/conversations/{conversation.conversation_id}/timeline",
            params={"cursor": cursor},
        )
        task_response = client.get(f"/api/v1/tasks/{task['task_id']}")

    assert after_response.status_code == task_response.status_code == 200
    after = after_response.json()["data"]["items"][0]
    assert (before["item_id"], before["anchor_at"]) == (
        after["item_id"],
        after["anchor_at"],
    )
    assert before["tool_runs"]["selected_tool_run"]["attempt_no"] == 1
    assert after["tool_runs"]["selected_tool_run"]["attempt_no"] == 2
    assert after["tool_runs"]["attempt_count"] == 2
    assert task_response.json()["data"]["tool_run_count"] == 2
    assert runtime.calls == 2


def test_old_cursor_keeps_successful_explanation_and_latest_failure_rules(
    api_harness,
) -> None:
    from backend.tests.api.test_assets import _MemoryStorage
    from backend.tests.api.test_explanation_outcomes import _Runtime
    from backend.tests.api.test_m8_explanation_retry import (
        _client_options,
        _insert_later_failed_explanation,
    )
    from materialsagent.infrastructure.llm.mock_explanation import (
        MockExplanationAdapter,
    )

    actor_id = "actor_timeline_explanation_retry"
    conversation = _seed_knowledge_prefix(api_harness, actor_id)
    runtime = _Runtime()
    storage = _MemoryStorage()
    explanation = MockExplanationAdapter(mode="failure")
    with api_harness.create_client(
        actor_id,
        **_client_options(
            api_harness,
            runtime,
            storage,
            explanation,
        ),
    ) as client:
        submitted = client.post(
            f"/api/v1/conversations/{conversation.conversation_id}/messages",
            headers={
                "Idempotency-Key": "timeline-initial-explanation"
            },
            json={
                "submission_mode": "NEW_TASK",
                "content_text": "完整合法 Tool 请求",
            },
        )
        assert submitted.status_code == 200
        data = submitted.json()["data"]
        first = client.get(
            f"/api/v1/conversations/{conversation.conversation_id}/timeline",
            params={"limit": 2},
        )
        cursor = first.json()["data"]["next_cursor"]
        before = client.get(
            f"/api/v1/conversations/{conversation.conversation_id}/timeline",
            params={"cursor": cursor},
        ).json()["data"]["items"][0]
        assert before["explanation"]["status"] == "FAILED"
        explanation.mode = "success"
        retried = client.post(
            "/api/v1/tool-results/"
            + data["result_summary"]["result_id"]
            + "/explanations",
            headers={"Idempotency-Key": "timeline-explanation-retry"},
            json={},
        )
        assert retried.status_code == 200
        after = client.get(
            f"/api/v1/conversations/{conversation.conversation_id}/timeline",
            params={"cursor": cursor},
        ).json()["data"]["items"][0]
        _insert_later_failed_explanation(
            api_harness,
            conversation_id=conversation.conversation_id,
            task_id=data["task"]["task_id"],
            result_id=data["result_summary"]["result_id"],
        )
        with_latest_failure = client.get(
            f"/api/v1/conversations/{conversation.conversation_id}/timeline",
            params={"cursor": cursor},
        ).json()["data"]["items"][0]

    assert before["anchor_at"] == after["anchor_at"]
    assert after["explanation"]["status"] == "SUCCEEDED"
    assert after["explanation"]["attempt_no"] == 2
    assert with_latest_failure["explanation"]["attempt_no"] == 2
    assert with_latest_failure["explanation"]["text"]
    assert with_latest_failure["latest_explanation_failure"][
        "attempt_no"
    ] == 3
    assert runtime.calls == 1
