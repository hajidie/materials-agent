from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

from fastapi.testclient import TestClient
import pytest

from materialsagent.domain.ports.unit_of_work import (
    DatabaseUnavailableError,
    PersistenceError,
)
from materialsagent.infrastructure.db.session import create_session_factory
from materialsagent.infrastructure.db.unit_of_work import SQLAlchemyUnitOfWork


BASE_TIME = datetime(2026, 7, 19, 0, 0, tzinfo=timezone.utc)


def _message_headers(**values: str) -> dict[str, str]:
    return {
        "Idempotency-Key": f"message-{uuid4().hex}",
        **values,
    }


def _assert_utc(value: str) -> None:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    assert parsed.tzinfo is not None
    assert parsed.utcoffset() == timezone.utc.utcoffset(parsed)
    assert value.endswith("Z")


def _assert_validation_error(response) -> None:
    assert response.status_code == 422
    body = response.json()
    assert body["request_id"].startswith("req_")
    assert body["error"]["code"] == "VALIDATION_FAILED"
    assert body["error"]["message"]
    assert isinstance(body["error"]["details"], list)
    assert set(body["resource"]) == {
        "conversation_id",
        "task_id",
        "tool_run_id",
        "result_id",
    }
    assert "actor_id" not in response.text


def test_create_conversation_accepts_null_and_trimmed_title_from_server_actor(
    api_harness,
) -> None:
    actor_id = "actor_local"
    foreign_actor_id = "actor_foreign"
    api_harness.persist_actor(actor_id)
    api_harness.persist_actor(foreign_actor_id)

    with api_harness.create_client(actor_id) as client:
        client.cookies.set("actor_id", foreign_actor_id)
        without_title = client.post(
            f"/api/v1/conversations?actor_id={foreign_actor_id}",
            json={},
            headers={
                "X-Actor-Id": foreign_actor_id,
                "X-User-Id": "user_client_selected",
            },
        )
        with_title = client.post(
            "/api/v1/conversations",
            json={"title": "  显式标题  "},
        )

    assert without_title.status_code == 201
    assert with_title.status_code == 201
    assert without_title.json()["request_id"] != with_title.json()["request_id"]
    for response, expected_title in (
        (without_title, None),
        (with_title, "显式标题"),
    ):
        body = response.json()
        assert set(body) == {"request_id", "data"}
        assert set(body["data"]) == {
            "conversation_id",
            "title",
            "created_at",
            "updated_at",
        }
        assert body["data"]["conversation_id"].startswith("conv_")
        assert body["data"]["title"] == expected_title
        _assert_utc(body["data"]["created_at"])
        _assert_utc(body["data"]["updated_at"])
        assert "actor_id" not in response.text

    first_row = api_harness.conversation_row(
        without_title.json()["data"]["conversation_id"]
    )
    assert first_row is not None
    assert first_row.actor_id == actor_id


@pytest.mark.parametrize(
    "payload",
    [
        {"title": "   "},
        {"title": 123},
        {"actor_id": "actor_foreign"},
    ],
)
def test_create_conversation_rejects_invalid_or_client_owned_fields(
    api_harness,
    payload: dict[str, object],
) -> None:
    actor_id = "actor_local"
    api_harness.persist_actor(actor_id)

    with api_harness.create_client(actor_id) as client:
        response = client.post("/api/v1/conversations", json=payload)

    _assert_validation_error(response)
    assert api_harness.counts()["conversation"] == 0


def test_create_conversation_maps_primary_key_collision_to_safe_409(
    api_harness,
) -> None:
    actor_id = "actor_local"
    api_harness.persist_actor(actor_id)
    api_harness.persist_conversation(
        actor_id,
        conversation_id="conv_collision",
    )

    def colliding_id(prefix: str) -> str:
        assert prefix == "conv"
        return "conv_collision"

    with api_harness.create_client(actor_id, id_factory=colliding_id) as client:
        response = client.post("/api/v1/conversations", json={})

    assert response.status_code == 409
    assert response.json()["error"] == {
        "code": "RESOURCE_CONFLICT",
        "message": "资源状态冲突。",
        "details": [],
    }
    assert api_harness.counts()["conversation"] == 1


def test_list_conversations_is_owned_stable_paginated_and_has_safe_preview(
    api_harness,
) -> None:
    actor_id = "actor_local"
    foreign_actor_id = "actor_foreign"
    api_harness.persist_actor(actor_id)
    api_harness.persist_actor(foreign_actor_id)
    shared_time = BASE_TIME + timedelta(hours=3)
    first = api_harness.persist_conversation(
        actor_id,
        conversation_id="conv_z",
        updated_at=shared_time,
    )
    second = api_harness.persist_conversation(
        actor_id,
        conversation_id="conv_a",
        title="第二个",
        updated_at=shared_time,
    )
    old = api_harness.persist_conversation(
        actor_id,
        conversation_id="conv_old",
        updated_at=BASE_TIME + timedelta(hours=2),
    )
    api_harness.persist_conversation(
        foreign_actor_id,
        conversation_id="conv_foreign",
        updated_at=BASE_TIME + timedelta(hours=4),
    )
    api_harness.persist_message_task(
        second,
        content_text="older",
        created_at=BASE_TIME + timedelta(minutes=2),
        message_id="msg_a",
        task_id="task_a",
    )
    api_harness.persist_message_task(
        second,
        content_text="  latest\n\tpreview  ",
        created_at=BASE_TIME + timedelta(minutes=2),
        message_id="msg_z",
        task_id="task_z",
    )
    api_harness.persist_message_task(
        old,
        content_text="预" * 81,
        created_at=BASE_TIME + timedelta(minutes=3),
    )

    with api_harness.create_client(actor_id) as client:
        page_one = client.get("/api/v1/conversations?limit=2")
        page_two = client.get(
            "/api/v1/conversations",
            params={"limit": 2, "cursor": page_one.json()["data"]["next_cursor"]},
        )

    assert page_one.status_code == 200
    assert page_two.status_code == 200
    assert [
        item["conversation_id"] for item in page_one.json()["data"]["items"]
    ] == [first.conversation_id, second.conversation_id]
    assert page_one.json()["data"]["items"][0]["last_activity_preview"] is None
    assert (
        page_one.json()["data"]["items"][1]["last_activity_preview"]
        == "latest preview"
    )
    assert page_one.json()["data"]["next_cursor"]
    assert [
        item["conversation_id"] for item in page_two.json()["data"]["items"]
    ] == [old.conversation_id]
    assert (
        page_two.json()["data"]["items"][0]["last_activity_preview"]
        == ("预" * 80) + "…"
    )
    assert page_two.json()["data"]["next_cursor"] is None
    assert "conv_foreign" not in page_one.text + page_two.text
    assert "actor_id" not in page_one.text + page_two.text


@pytest.mark.parametrize(
    ("params", "expected_status"),
    [
        ({"cursor": "damaged-@@@"}, 422),
        ({"limit": 0}, 422),
        ({"limit": 101}, 422),
    ],
)
def test_list_conversations_rejects_invalid_pagination(
    api_harness,
    params: dict[str, object],
    expected_status: int,
) -> None:
    actor_id = "actor_local"
    api_harness.persist_actor(actor_id)

    with api_harness.create_client(actor_id) as client:
        response = client.get("/api/v1/conversations", params=params)

    assert response.status_code == expected_status
    assert response.json()["error"]["code"] == "VALIDATION_FAILED"


def test_new_task_submission_persists_orchestrated_facts_and_first_title(
    api_harness,
) -> None:
    actor_id = "actor_local"
    foreign_actor_id = "actor_foreign"
    api_harness.persist_actor(actor_id)
    api_harness.persist_actor(foreign_actor_id)
    conversation = api_harness.persist_conversation(actor_id)

    with api_harness.create_client(actor_id) as client:
        response = client.post(
            f"/api/v1/conversations/{conversation.conversation_id}/messages",
            json={
                "submission_mode": "NEW_TASK",
                "content_text": "  请帮我分析这一材料问题。  ",
            },
            headers=_message_headers(**{"X-Actor-Id": foreign_actor_id}),
        )

    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"request_id", "data"}
    assert set(body["data"]) == {
        "conversation_id",
        "user_message",
        "task",
        "assistant_message",
        "needs_input",
        "result_summary",
        "explanation",
        "latest_explanation_failure",
        "idempotency_replayed",
    }
    assert body["data"]["conversation_id"] == conversation.conversation_id
    assert body["data"]["user_message"]["role"] == "USER"
    assert body["data"]["user_message"]["content_text"] == "请帮我分析这一材料问题。"
    assert body["data"]["task"]["task_type"] == "KNOWLEDGE_QA"
    assert body["data"]["task"]["status"] == "SUCCEEDED"
    assert body["data"]["task"]["selected_tool_run_id"] is None
    assert body["data"]["task"]["selected_result_id"] is None
    assert body["data"]["assistant_message"]["role"] == "ASSISTANT"
    assert body["data"]["assistant_message"]["content_text"]
    assert body["data"]["needs_input"] is None
    assert body["data"]["result_summary"] is None
    assert body["data"]["explanation"] is None
    assert body["data"]["idempotency_replayed"] is False
    assert "actor_id" not in response.text
    counts = api_harness.counts()
    assert counts == {
        "conversation": 1,
        "message": 2,
        "task": 1,
        "task_input_revision": 0,
    }
    message = next(
        row for row in api_harness.message_rows() if row.role == "USER"
    )
    task = api_harness.task_rows()[0]
    updated = api_harness.conversation_row(conversation.conversation_id)
    assert updated is not None
    assert message.actor_id == task.actor_id == updated.actor_id == actor_id
    assert message.conversation_id == task.conversation_id == conversation.conversation_id
    assert message.task_id == task.task_id
    assert message.request_id == body["request_id"]
    assert updated.updated_at > BASE_TIME
    assert updated.title == "请帮我分析这一材料问题。"


def test_new_task_submission_keeps_conversation_updated_at_monotonic(
    api_harness,
) -> None:
    actor_id = "actor_local"
    api_harness.persist_actor(actor_id)
    newer_time = BASE_TIME + timedelta(hours=2)
    conversation = api_harness.persist_conversation(
        actor_id,
        updated_at=newer_time,
    )

    with api_harness.create_client(actor_id) as client:
        response = client.post(
            f"/api/v1/conversations/{conversation.conversation_id}/messages",
            headers=_message_headers(),
            json={"submission_mode": "NEW_TASK", "content_text": "旧时钟标题"},
        )

    assert response.status_code == 200
    assert api_harness.counts() == {
        "conversation": 1,
        "message": 2,
        "task": 1,
        "task_input_revision": 0,
    }
    updated = api_harness.conversation_row(conversation.conversation_id)
    assert updated is not None
    assert updated.updated_at == newer_time
    assert updated.title == "旧时钟标题"


def test_second_message_and_explicit_title_are_never_overwritten(
    api_harness,
) -> None:
    actor_id = "actor_local"
    api_harness.persist_actor(actor_id)
    automatic = api_harness.persist_conversation(actor_id)
    explicit = api_harness.persist_conversation(actor_id, title="显式标题")

    with api_harness.create_client(actor_id) as client:
        first = client.post(
            f"/api/v1/conversations/{automatic.conversation_id}/messages",
            headers=_message_headers(),
            json={"submission_mode": "NEW_TASK", "content_text": "第一条消息"},
        )
        second = client.post(
            f"/api/v1/conversations/{automatic.conversation_id}/messages",
            headers=_message_headers(),
            json={"submission_mode": "NEW_TASK", "content_text": "第二条消息"},
        )
        explicit_response = client.post(
            f"/api/v1/conversations/{explicit.conversation_id}/messages",
            headers=_message_headers(),
            json={"submission_mode": "NEW_TASK", "content_text": "不会覆盖"},
        )

    assert [first.status_code, second.status_code, explicit_response.status_code] == [
        200,
        200,
        200,
    ]
    assert api_harness.conversation_row(automatic.conversation_id).title == "第一条消息"
    assert api_harness.conversation_row(explicit.conversation_id).title == "显式标题"


@pytest.mark.parametrize(
    "payload",
    [
        {"submission_mode": "NEW_TASK", "content_text": "   "},
        {"submission_mode": "NEW_TASK", "content_text": 123},
        {
            "submission_mode": "NEW_TASK",
            "content_text": "valid",
            "target_task_id": "task_client",
        },
        {"submission_mode": "SUPPLEMENT_TASK", "content_text": "valid"},
        {
            "submission_mode": "NEW_TASK",
            "content_text": "valid",
            "actor_id": "actor_client",
        },
        {
            "submission_mode": "NEW_TASK",
            "content_text": "valid",
            "conversation_id": "conv_client",
        },
    ],
)
def test_message_submission_rejects_invalid_m3_inputs_without_writes(
    api_harness,
    payload: dict[str, object],
) -> None:
    actor_id = "actor_local"
    api_harness.persist_actor(actor_id)
    conversation = api_harness.persist_conversation(actor_id)
    original_updated_at = conversation.updated_at

    with api_harness.create_client(actor_id) as client:
        response = client.post(
            f"/api/v1/conversations/{conversation.conversation_id}/messages",
            headers=_message_headers(),
            json=payload,
        )

    _assert_validation_error(response)
    counts = api_harness.counts()
    assert counts["message"] == counts["task"] == counts["task_input_revision"] == 0
    assert (
        api_harness.conversation_row(conversation.conversation_id).updated_at
        == original_updated_at
    )


def test_foreign_and_missing_conversation_have_indistinguishable_safe_404(
    api_harness,
) -> None:
    actor_id = "actor_local"
    foreign_actor_id = "actor_foreign"
    api_harness.persist_actor(actor_id)
    api_harness.persist_actor(foreign_actor_id)
    foreign = api_harness.persist_conversation(foreign_actor_id)
    payload = {"submission_mode": "NEW_TASK", "content_text": "valid"}

    with api_harness.create_client(actor_id) as client:
        foreign_response = client.post(
            f"/api/v1/conversations/{foreign.conversation_id}/messages",
            headers=_message_headers(),
            json=payload,
        )
        missing_response = client.post(
            "/api/v1/conversations/conv_missing/messages",
            headers=_message_headers(),
            json=payload,
        )

    for response in (foreign_response, missing_response):
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "RESOURCE_NOT_FOUND"
        assert response.json()["error"]["details"] == []
    assert foreign_response.json()["error"] == missing_response.json()["error"]
    assert api_harness.counts()["message"] == 0
    assert api_harness.counts()["task"] == 0


def test_title_generator_failure_does_not_rollback_message_or_task(
    api_harness,
) -> None:
    actor_id = "actor_local"
    api_harness.persist_actor(actor_id)
    conversation = api_harness.persist_conversation(actor_id)

    def fail_title(_: str) -> str:
        raise RuntimeError("private title detail")

    with api_harness.create_client(actor_id, title_generator=fail_title) as client:
        response = client.post(
            f"/api/v1/conversations/{conversation.conversation_id}/messages",
            headers=_message_headers(),
            json={"submission_mode": "NEW_TASK", "content_text": "valid"},
        )

    assert response.status_code == 200
    assert api_harness.counts()["message"] == 2
    assert api_harness.counts()["task"] == 1
    assert api_harness.conversation_row(conversation.conversation_id).title is None
    assert "private title detail" not in response.text


class _CommitFailureUnitOfWork(SQLAlchemyUnitOfWork):
    def commit(self) -> None:
        self.rollback()
        raise DatabaseUnavailableError("Database unavailable.")


class _InternalCommitFailureUnitOfWork(SQLAlchemyUnitOfWork):
    def commit(self) -> None:
        self.rollback()
        raise PersistenceError("Persistence operation failed.")


class _SelectiveFailureFactory:
    def __init__(
        self,
        engine,
        fail_on_calls: set[int],
        failure_type: type[SQLAlchemyUnitOfWork] = _CommitFailureUnitOfWork,
    ) -> None:
        self._session_factory = create_session_factory(engine)
        self._fail_on_calls = fail_on_calls
        self._failure_type = failure_type
        self.call_count = 0
        self.instances: list[SQLAlchemyUnitOfWork] = []

    def __call__(self) -> SQLAlchemyUnitOfWork:
        self.call_count += 1
        unit_of_work: SQLAlchemyUnitOfWork
        if self.call_count in self._fail_on_calls:
            unit_of_work = self._failure_type(self._session_factory)
        else:
            unit_of_work = SQLAlchemyUnitOfWork(self._session_factory)
        self.instances.append(unit_of_work)
        return unit_of_work


def test_title_persistence_failure_does_not_rollback_message_or_task(
    api_harness,
) -> None:
    actor_id = "actor_local"
    api_harness.persist_actor(actor_id)
    conversation = api_harness.persist_conversation(actor_id)
    failure_factory = _SelectiveFailureFactory(api_harness.engine, {2})

    with api_harness.create_client(
        actor_id,
        unit_of_work_factory=failure_factory,
    ) as client:
        response = client.post(
            f"/api/v1/conversations/{conversation.conversation_id}/messages",
            headers=_message_headers(),
            json={"submission_mode": "NEW_TASK", "content_text": "valid"},
        )

    assert response.status_code == 200
    assert api_harness.counts()["message"] == 2
    assert api_harness.counts()["task"] == 1
    assert api_harness.conversation_row(conversation.conversation_id).title is None
    assert failure_factory.call_count == 6
    assert all(unit_of_work.session is None for unit_of_work in failure_factory.instances)


def test_stale_title_update_does_not_overwrite_concurrent_winner(
    api_harness,
) -> None:
    from materialsagent.infrastructure.db.conversation_task import (
        ConversationRow,
    )

    actor_id = "actor_local"
    api_harness.persist_actor(actor_id)
    conversation = api_harness.persist_conversation(actor_id)
    session_factory = create_session_factory(api_harness.engine)

    with SQLAlchemyUnitOfWork(session_factory) as stale_unit_of_work:
        assert stale_unit_of_work.session is not None
        cached_row = stale_unit_of_work.session.get(
            ConversationRow,
            conversation.conversation_id,
        )
        assert cached_row is not None
        assert cached_row.title is None
        stale = stale_unit_of_work.conversations.get_owned(
            conversation.conversation_id,
            actor_id,
        )
        assert stale is not None

        with SQLAlchemyUnitOfWork(session_factory) as winner_unit_of_work:
            winner = winner_unit_of_work.conversations.get_owned(
                conversation.conversation_id,
                actor_id,
            )
            assert winner is not None
            winner.title = "并发胜者"
            winner_unit_of_work.conversations.update(winner)
            winner_unit_of_work.commit()

        stale.title = "陈旧覆盖"
        stale_unit_of_work.conversations.update(stale)
        stale_unit_of_work.commit()

    assert (
        api_harness.conversation_row(conversation.conversation_id).title
        == "并发胜者"
    )


def test_stale_repository_update_keeps_newer_database_updated_at(
    api_harness,
) -> None:
    actor_id = "actor_local"
    api_harness.persist_actor(actor_id)
    newer_time = BASE_TIME + timedelta(hours=2)
    conversation = api_harness.persist_conversation(
        actor_id,
        updated_at=newer_time,
    )
    conversation.updated_at = BASE_TIME + timedelta(hours=1)

    with api_harness.unit_of_work_factory() as unit_of_work:
        updated = unit_of_work.conversations.update(conversation)
        unit_of_work.commit()

    assert updated is not None
    assert updated.updated_at == newer_time
    row = api_harness.conversation_row(conversation.conversation_id)
    assert row is not None
    assert row.updated_at == newer_time


def test_real_database_commit_failure_rolls_back_all_and_closes_session(
    api_harness,
) -> None:
    actor_id = "actor_local"
    api_harness.persist_actor(actor_id)
    conversation = api_harness.persist_conversation(actor_id)
    original_updated_at = conversation.updated_at
    failure_factory = _SelectiveFailureFactory(api_harness.engine, {1})

    with api_harness.create_client(
        actor_id,
        unit_of_work_factory=failure_factory,
    ) as client:
        failed = client.post(
            f"/api/v1/conversations/{conversation.conversation_id}/messages",
            headers=_message_headers(),
            json={"submission_mode": "NEW_TASK", "content_text": "valid"},
        )

    assert failed.status_code == 503
    assert failed.json()["error"]["code"] == "DEPENDENCY_UNAVAILABLE"
    counts = api_harness.counts()
    assert counts["message"] == counts["task"] == 0
    assert (
        api_harness.conversation_row(conversation.conversation_id).updated_at
        == original_updated_at
    )
    assert all(unit_of_work.session is None for unit_of_work in failure_factory.instances)

    with api_harness.create_client(actor_id) as healthy_client:
        recovered = healthy_client.post(
            f"/api/v1/conversations/{conversation.conversation_id}/messages",
            headers=_message_headers(),
            json={"submission_mode": "NEW_TASK", "content_text": "recovered"},
        )
    assert recovered.status_code == 200


def test_generic_persistence_failure_returns_safe_internal_error(
    api_harness,
) -> None:
    actor_id = "actor_local"
    api_harness.persist_actor(actor_id)
    conversation = api_harness.persist_conversation(actor_id)
    failure_factory = _SelectiveFailureFactory(
        api_harness.engine,
        {1},
        _InternalCommitFailureUnitOfWork,
    )

    with api_harness.create_client(
        actor_id,
        unit_of_work_factory=failure_factory,
    ) as client:
        response = client.post(
            f"/api/v1/conversations/{conversation.conversation_id}/messages",
            headers=_message_headers(),
            json={"submission_mode": "NEW_TASK", "content_text": "valid"},
        )

    assert response.status_code == 500
    assert response.json()["error"] == {
        "code": "INTERNAL_ERROR",
        "message": "内部处理失败。",
        "details": [],
    }
    assert api_harness.counts()["message"] == 0
    assert api_harness.counts()["task"] == 0
    assert all(unit_of_work.session is None for unit_of_work in failure_factory.instances)


def test_invalid_json_uses_safe_400_without_echoing_body(
    api_harness,
) -> None:
    actor_id = "actor_local"
    api_harness.persist_actor(actor_id)
    secret = "private-invalid-json-secret"

    with api_harness.create_client(actor_id) as client:
        response = client.post(
            "/api/v1/conversations",
            content='{"title": "' + secret,
            headers={"Content-Type": "application/json"},
        )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "INVALID_REQUEST_BODY"
    assert secret not in response.text
    assert "Traceback" not in response.text
    assert "C:\\Users" not in response.text


def test_missing_business_database_configuration_is_safe_while_live_works() -> None:
    from materialsagent.infrastructure.config import load_settings
    from materialsagent.main import create_app

    app = create_app(
        settings=load_settings(
            {
                "LOCAL_ACTOR_ID": "actor_local",
            }
        )
    )
    with TestClient(app, raise_server_exceptions=False) as client:
        live = client.get("/api/v1/health/live")
        business = client.post("/api/v1/conversations", json={})

    assert live.status_code == 200
    assert live.json()["status"] == "LIVE"
    assert business.status_code == 503
    assert business.json()["error"]["code"] == "DEPENDENCY_UNAVAILABLE"


def test_imports_and_create_app_have_no_external_or_database_side_effects(
    api_harness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import importlib

    import materialsagent.application.bootstrap as bootstrap_module
    import materialsagent.infrastructure.storage.minio as minio_module
    from sqlalchemy.engine import Engine

    before = api_harness.counts()
    for module_name in (
        "materialsagent.application.context",
        "materialsagent.application.errors",
        "materialsagent.application.conversations",
        "materialsagent.application.messages",
        "materialsagent.application.tasks",
        "materialsagent.api.dependencies",
        "materialsagent.api.routes.conversations",
        "materialsagent.api.routes.tasks",
        "materialsagent.main",
    ):
        importlib.import_module(module_name)

    def forbidden(*_args: object, **_kwargs: object) -> Any:
        pytest.fail("create_app performed an external side effect")

    with monkeypatch.context() as scoped:
        scoped.setattr(Engine, "connect", forbidden)
        scoped.setattr(bootstrap_module, "ensure_local_actor", forbidden)
        scoped.setattr(minio_module, "create_minio_storage", forbidden)
        from materialsagent.main import create_app

        app = create_app(settings=api_harness.settings)
        with TestClient(app, raise_server_exceptions=False):
            pass

    assert api_harness.counts() == before


def test_create_app_disposes_only_its_owned_business_engine(
    api_harness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import materialsagent.main as main_module
    from materialsagent.application.readiness import ReadinessService

    class FakeEngine:
        def __init__(self) -> None:
            self.disposed = False

        def dispose(self) -> None:
            self.disposed = True

    fake_engine = FakeEngine()
    monkeypatch.setattr(
        main_module,
        "create_engine_from_settings",
        lambda _settings: fake_engine,
    )
    monkeypatch.setattr(
        main_module,
        "create_session_factory",
        lambda _engine: object(),
    )
    app = main_module.create_app(
        settings=api_harness.settings,
        readiness_service=ReadinessService(
            postgresql_probe=lambda: True,
            object_storage_probe=lambda: True,
        ),
    )
    assert fake_engine.disposed is False

    with TestClient(app, raise_server_exceptions=False) as client:
        assert client.get("/api/v1/health/live").status_code == 200

    assert fake_engine.disposed is True
