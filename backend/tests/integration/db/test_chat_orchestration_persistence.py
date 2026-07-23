from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from sqlalchemy import delete, func, select, text
from sqlalchemy.engine import Engine

from materialsagent.application.chat_orchestration import ChatOrchestrationService
from materialsagent.application.context import ActorContext
from materialsagent.application.messages import PreparedSubmission
from materialsagent.domain.models.actor import Actor
from materialsagent.domain.models.conversation import Conversation
from materialsagent.domain.models.message import Message
from materialsagent.domain.models.task import Task
from materialsagent.domain.models.task_input_revision import TaskInputRevision
from materialsagent.domain.ports.chat_orchestration import (
    ChatOrchestrationProviderError,
    ChatOrchestrationTimeoutError,
    KnowledgeAnswer,
)
from materialsagent.domain.ports.unit_of_work import PersistenceError
from materialsagent.infrastructure.db.conversation_task import (
    MessageRow,
    TaskInputRevisionRow,
    TaskRow,
)
from materialsagent.infrastructure.db.llm_call import LLMCallRow
from materialsagent.infrastructure.db.session import create_session_factory
from materialsagent.infrastructure.db.unit_of_work import SQLAlchemyUnitOfWork
from materialsagent.infrastructure.llm.mock import MockChatOrchestrationAdapter


BASE_TIME = datetime(2026, 7, 19, 12, 0, tzinfo=timezone.utc)


class _Clock:
    def __init__(self) -> None:
        self._index = 0

    def __call__(self) -> datetime:
        value = BASE_TIME + timedelta(seconds=self._index)
        self._index += 1
        return value


class _TrackingUnitOfWork(SQLAlchemyUnitOfWork):
    def __init__(self, owner: _Factory, *, fail_commit: bool = False) -> None:
        super().__init__(owner.session_factory)
        self._owner = owner
        self._fail_commit = fail_commit

    def __enter__(self) -> _TrackingUnitOfWork:
        super().__enter__()
        self._owner.active += 1
        return self

    def __exit__(self, *args: object) -> None:
        try:
            super().__exit__(*args)
        finally:
            self._owner.active -= 1

    def commit(self) -> None:
        if self._fail_commit:
            self.rollback()
            raise PersistenceError("Persistence operation failed.")
        super().commit()


class _Factory:
    def __init__(
        self,
        engine: Engine,
        *,
        fail_calls: set[int] | None = None,
    ) -> None:
        self.session_factory = create_session_factory(engine)
        self.fail_calls = fail_calls or set()
        self.call_count = 0
        self.active = 0
        self.instances: list[_TrackingUnitOfWork] = []

    def __call__(self) -> _TrackingUnitOfWork:
        self.call_count += 1
        unit_of_work = _TrackingUnitOfWork(
            self,
            fail_commit=self.call_count in self.fail_calls,
        )
        self.instances.append(unit_of_work)
        return unit_of_work


def _id_factory(prefix: str) -> str:
    return {
        "llm": "llm_integration",
        "msg": "msg_assistant_integration",
        "revision": "revision_integration",
    }[prefix]


def _persist_submission(
    engine: Engine,
    *,
    actor_id: str = "actor_integration",
) -> tuple[ActorContext, PreparedSubmission]:
    actor = ActorContext(actor_id=actor_id, user_id=None)
    conversation = Conversation(
        conversation_id="conv_integration",
        actor_id=actor_id,
        title=None,
        created_at=BASE_TIME,
        updated_at=BASE_TIME,
    )
    task = Task.pending(
        task_id="task_integration",
        conversation_id=conversation.conversation_id,
        actor_id=actor_id,
        created_at=BASE_TIME,
    )
    message = Message.user(
        message_id="msg_user_integration",
        conversation_id=conversation.conversation_id,
        task_id=task.task_id,
        actor_id=actor_id,
        request_id="req_integration",
        content_text="测试消息",
        created_at=BASE_TIME,
    )
    factory = lambda: SQLAlchemyUnitOfWork(create_session_factory(engine))
    with factory() as unit_of_work:
        unit_of_work.actors.add(
            Actor.local_anonymous(actor_id, created_at=BASE_TIME)
        )
        unit_of_work.conversations.add(conversation)
        unit_of_work.tasks.add(task)
        unit_of_work.messages.add(message)
        unit_of_work.commit()
    return actor, PreparedSubmission(
        conversation_id=conversation.conversation_id,
        user_message=message,
        task=task,
    )


def _tool_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "route": "TOOL_EXECUTION",
        "tool_id": "zta35g_sem_virtual_lab",
        "material": "ZTA35G",
        "candidate_parameters": {
            "solution_temperature": {"value": 1000, "unit": "°C"},
            "solution_time": {"value": 180, "unit": "min"},
            "aging_temperature": {"value": 730, "unit": "°C"},
            "aging_time": {"value": 3, "unit": "h"},
        },
        "requested_outputs": ["sem_image"],
    }
    payload.update(overrides)
    return payload


def _service(
    factory: _Factory,
    responder,
) -> ChatOrchestrationService:
    return ChatOrchestrationService(
        factory,
        MockChatOrchestrationAdapter(responder),
        clock=_Clock(),
        id_factory=_id_factory,
    )


def test_knowledge_path_persists_owned_message_call_and_task_atomically(
    migrated_database_engine: Engine,
) -> None:
    actor, submission = _persist_submission(migrated_database_engine)
    factory = _Factory(migrated_database_engine)
    adapter_calls = 0

    def responder(_input: object) -> dict[str, object]:
        nonlocal adapter_calls
        assert factory.active == 0
        adapter_calls += 1
        return {"route": "KNOWLEDGE_ANSWER", "answer_text": "持久化知识回答。"}

    projection = _service(factory, responder).orchestrate_submission(
        actor,
        submission,
    )

    assert adapter_calls == 1
    assert projection.task.current_status == "SUCCEEDED"
    assert projection.assistant_message is not None
    with migrated_database_engine.connect() as connection:
        message_rows = connection.execute(
            select(
                MessageRow.actor_id,
                MessageRow.conversation_id,
                MessageRow.task_id,
                MessageRow.request_id,
                MessageRow.llm_call_id,
            ).order_by(MessageRow.role.desc())
        ).all()
        assert len(message_rows) == 2
        assistant = next(row for row in message_rows if row.llm_call_id is not None)
        assert assistant.actor_id == actor.actor_id
        assert assistant.conversation_id == submission.conversation_id
        assert assistant.task_id == submission.task.task_id
        assert assistant.request_id == submission.user_message.request_id
        assert assistant.llm_call_id == "llm_integration"
        assert connection.scalar(
            select(func.count()).select_from(TaskInputRevisionRow)
        ) == 0


def test_needs_input_and_tool_outcomes_persist_formal_revision_and_no_tool_table(
    migrated_database_engine: Engine,
) -> None:
    actor, submission = _persist_submission(migrated_database_engine)
    factory = _Factory(migrated_database_engine)
    parameters = dict(_tool_payload()["candidate_parameters"])
    parameters["aging_temperature"] = None
    projection = _service(
        factory,
        lambda _: {
            "route": "NEEDS_INPUT",
            "tool_id": "zta35g_sem_virtual_lab",
            "material": "ZTA35G",
            "candidate_parameters": parameters,
            "missing_fields": ["solution_time"],
            "ambiguous_fields": [],
            "follow_up_suggestion": "不可信提示",
            "requested_outputs": ["sem_image"],
        },
    ).orchestrate_submission(actor, submission)

    assert projection.task.current_status == "NEEDS_INPUT"
    assert projection.revision is not None
    assert projection.revision.missing_fields == ["aging_temperature"]
    assert projection.revision.source_message_ids == [
        submission.user_message.message_id
    ]
    with migrated_database_engine.connect() as connection:
        assert connection.scalar(text("SELECT count(*) FROM tool_run")) == 0
        assert connection.scalar(text("SELECT to_regclass('public.asset')")) == "asset"
        assert connection.scalar(text("SELECT count(*) FROM asset")) == 0
        assert connection.scalar(
            text("SELECT to_regclass('public.tool_result')")
        ) == "tool_result"
        assert connection.scalar(text("SELECT count(*) FROM tool_result")) == 0


@pytest.mark.parametrize(
    ("payload", "expected_http", "expected_task_error"),
    [
        (_tool_payload(material="OTHER"), 422, "VALIDATION_FAILED"),
        (_tool_payload(), 503, "TOOL_UNAVAILABLE"),
    ],
)
def test_hard_invalid_and_complete_valid_tool_candidates_are_committed_before_error(
    migrated_database_engine: Engine,
    payload: dict[str, object],
    expected_http: int,
    expected_task_error: str,
) -> None:
    actor, submission = _persist_submission(migrated_database_engine)
    factory = _Factory(migrated_database_engine)

    with pytest.raises(Exception) as captured:
        _service(factory, lambda _: payload).orchestrate_submission(
            actor,
            submission,
        )

    assert captured.value.status_code == expected_http
    with migrated_database_engine.connect() as connection:
        call = connection.execute(select(LLMCallRow)).one()
        task = connection.execute(select(TaskRow)).one()
        revision = connection.execute(select(TaskInputRevisionRow)).one()
        assert call.status == "SUCCEEDED"
        assert task.current_status == "FAILED"
        assert task.error_code == expected_task_error
        assert revision.revision == 1
        assert revision.normalized_input["solution_time"] == {
            "value": 3,
            "unit": "h",
        }
        assert connection.scalar(
            select(func.count()).select_from(MessageRow)
        ) == 1


@pytest.mark.parametrize(
    "failure",
    [
        ChatOrchestrationTimeoutError("private timeout"),
        ChatOrchestrationProviderError("private provider"),
        None,
    ],
)
def test_timeout_provider_and_protocol_failures_persist_safe_terminal_facts(
    migrated_database_engine: Engine,
    failure: Exception | None,
) -> None:
    actor, submission = _persist_submission(migrated_database_engine)
    factory = _Factory(migrated_database_engine)

    def responder(_: object) -> dict[str, object]:
        if failure is not None:
            raise failure
        return {"route": "UNKNOWN", "provider_payload": "SECRET"}

    with pytest.raises(Exception):
        _service(factory, responder).orchestrate_submission(actor, submission)

    with migrated_database_engine.connect() as connection:
        call = connection.execute(select(LLMCallRow)).one()
        task = connection.execute(select(TaskRow)).one()
        assert call.status == "FAILED"
        assert call.structured_output_summary is None
        assert task.current_status == "FAILED"
        assert connection.scalar(
            select(func.count()).select_from(MessageRow)
        ) == 1
        assert connection.scalar(
            select(func.count()).select_from(TaskInputRevisionRow)
        ) == 0
        assert "private" not in (call.safe_error_message or "").lower()


def test_cross_actor_source_message_is_rejected_before_llm_call(
    migrated_database_engine: Engine,
) -> None:
    actor, submission = _persist_submission(migrated_database_engine)
    foreign_actor_id = "actor_foreign_integration"
    foreign_conversation = replace(
        Conversation(
            conversation_id="conv_foreign_integration",
            actor_id=foreign_actor_id,
            title=None,
            created_at=BASE_TIME,
            updated_at=BASE_TIME,
        )
    )
    foreign_task = Task.pending(
        task_id="task_foreign_integration",
        conversation_id=foreign_conversation.conversation_id,
        actor_id=foreign_actor_id,
        created_at=BASE_TIME,
    )
    foreign_message = Message.user(
        message_id="msg_foreign_integration",
        conversation_id=foreign_conversation.conversation_id,
        task_id=foreign_task.task_id,
        actor_id=foreign_actor_id,
        request_id=submission.user_message.request_id,
        content_text="foreign",
        created_at=BASE_TIME,
    )
    with SQLAlchemyUnitOfWork(
        create_session_factory(migrated_database_engine)
    ) as unit_of_work:
        unit_of_work.actors.add(
            Actor.local_anonymous(foreign_actor_id, created_at=BASE_TIME)
        )
        unit_of_work.conversations.add(foreign_conversation)
        unit_of_work.tasks.add(foreign_task)
        unit_of_work.messages.add(foreign_message)
        unit_of_work.commit()
    forged_submission = replace(submission, user_message=foreign_message)
    calls = 0

    def responder(_: object) -> dict[str, object]:
        nonlocal calls
        calls += 1
        return {"route": "KNOWLEDGE_ANSWER", "answer_text": "不应调用。"}

    with pytest.raises(Exception) as captured:
        _service(_Factory(migrated_database_engine), responder).orchestrate_submission(
            actor,
            forged_submission,
        )

    assert captured.value.code == "RESOURCE_CONFLICT"
    assert calls == 0
    with migrated_database_engine.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(LLMCallRow)) == 0


def test_conditional_task_update_and_repeated_finalize_preserve_first_terminal_facts(
    migrated_database_engine: Engine,
) -> None:
    actor, submission = _persist_submission(migrated_database_engine)
    factory = _Factory(migrated_database_engine)
    service = _service(
        factory,
        lambda _: {"route": "KNOWLEDGE_ANSWER", "answer_text": "第一答案。"},
    )
    first = service.orchestrate_submission(actor, submission)

    second = service.finalize_result(
        actor,
        submission,
        llm_call_id="llm_integration",
        result=KnowledgeAnswer("第二答案。"),
    )
    stale_running = replace(
        second.task,
        current_status="RUNNING",
        completed_at=None,
        error_code=None,
        safe_error_message=None,
    )
    with factory() as unit_of_work:
        assert unit_of_work.tasks.update(
            stale_running,
            expected_status="RUNNING",
        ) is None
        unit_of_work.commit()

    assert second == first
    with migrated_database_engine.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(MessageRow)) == 2
        assert connection.scalar(
            select(func.count()).select_from(TaskInputRevisionRow)
        ) == 0


def test_finalize_commit_failure_rolls_back_terminal_entities(
    migrated_database_engine: Engine,
) -> None:
    actor, submission = _persist_submission(migrated_database_engine)
    factory = _Factory(migrated_database_engine, fail_calls={3})

    with pytest.raises(Exception) as captured:
        _service(
            factory,
            lambda _: {"route": "KNOWLEDGE_ANSWER", "answer_text": "内存答案。"},
        ).orchestrate_submission(actor, submission)

    assert captured.value.code == "INTERNAL_ERROR"
    assert all(instance.session is None for instance in factory.instances)
    with migrated_database_engine.connect() as connection:
        call = connection.execute(select(LLMCallRow)).one()
        task = connection.execute(select(TaskRow)).one()
        assert call.status == "RUNNING"
        assert task.current_status == "RUNNING"
        assert connection.scalar(select(func.count()).select_from(MessageRow)) == 1


def test_postgresql_repeat_rejects_knowledge_without_assistant(
    migrated_database_engine: Engine,
) -> None:
    actor, submission = _persist_submission(migrated_database_engine)
    factory = _Factory(migrated_database_engine)
    service = _service(
        factory,
        lambda _: {"route": "KNOWLEDGE_ANSWER", "answer_text": "第一答案。"},
    )
    service.orchestrate_submission(actor, submission)
    with migrated_database_engine.begin() as connection:
        connection.execute(
            delete(MessageRow).where(MessageRow.llm_call_id == "llm_integration")
        )

    with pytest.raises(Exception) as captured:
        service.finalize_result(
            actor,
            submission,
            llm_call_id="llm_integration",
            result=KnowledgeAnswer("陈旧答案。"),
        )

    assert captured.value.code == "RESOURCE_CONFLICT"
    with migrated_database_engine.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(MessageRow)) == 1
        task = connection.execute(select(TaskRow)).one()
        call = connection.execute(select(LLMCallRow)).one()
        assert task.current_status == "SUCCEEDED"
        assert call.status == "SUCCEEDED"


def test_postgresql_repeat_rejects_two_revisions_for_same_call(
    migrated_database_engine: Engine,
) -> None:
    actor, submission = _persist_submission(migrated_database_engine)
    factory = _Factory(migrated_database_engine)
    service = _service(factory, lambda _: _tool_payload())
    with pytest.raises(Exception):
        service.orchestrate_submission(actor, submission)
    with migrated_database_engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO task_input_revision (
                    task_input_revision_id,
                    task_id,
                    request_id,
                    source_llm_call_id,
                    source_message_ids,
                    revision,
                    raw_input,
                    normalized_input,
                    missing_fields,
                    ambiguous_fields,
                    validation_errors,
                    created_at
                )
                SELECT
                    'revision_duplicate_integration',
                    task_id,
                    request_id,
                    source_llm_call_id,
                    source_message_ids,
                    2,
                    raw_input,
                    normalized_input,
                    missing_fields,
                    ambiguous_fields,
                    validation_errors,
                    created_at
                FROM task_input_revision
                WHERE task_input_revision_id = 'revision_integration'
                """
            )
        )

    with pytest.raises(Exception) as captured:
        service.finalize_result(
            actor,
            submission,
            llm_call_id="llm_integration",
            result=KnowledgeAnswer("陈旧答案。"),
        )

    assert captured.value.code == "RESOURCE_CONFLICT"
    with migrated_database_engine.connect() as connection:
        assert connection.scalar(
            select(func.count()).select_from(TaskInputRevisionRow)
        ) == 2
        task = connection.execute(select(TaskRow)).one()
        call = connection.execute(select(LLMCallRow)).one()
        assert task.error_code == "TOOL_UNAVAILABLE"
        assert call.status == "SUCCEEDED"


def test_postgresql_repeat_rejects_cross_task_revision_for_same_call(
    migrated_database_engine: Engine,
) -> None:
    actor, submission = _persist_submission(migrated_database_engine)
    factory = _Factory(migrated_database_engine)
    service = _service(factory, lambda _: _tool_payload())
    with pytest.raises(Exception):
        service.orchestrate_submission(actor, submission)
    with factory() as unit_of_work:
        unit_of_work.tasks.add(
            Task.pending(
                task_id="task_cross_integration",
                conversation_id=submission.conversation_id,
                actor_id=actor.actor_id,
                created_at=BASE_TIME + timedelta(seconds=10),
            )
        )
        unit_of_work.task_input_revisions.add(
            TaskInputRevision(
                task_input_revision_id="revision_cross_integration",
                task_id="task_cross_integration",
                request_id=submission.user_message.request_id,
                source_llm_call_id="llm_integration",
                source_message_ids=[submission.user_message.message_id],
                revision=1,
                raw_input={"cross_task": True},
                normalized_input={},
                missing_fields=[],
                ambiguous_fields=[],
                validation_errors=[],
                created_at=BASE_TIME + timedelta(seconds=10),
            )
        )
        unit_of_work.commit()

    with pytest.raises(Exception) as captured:
        service.finalize_result(
            actor,
            submission,
            llm_call_id="llm_integration",
            result=KnowledgeAnswer("陈旧答案。"),
        )

    assert captured.value.code == "RESOURCE_CONFLICT"
    with migrated_database_engine.connect() as connection:
        assert connection.scalar(
            select(func.count()).select_from(TaskInputRevisionRow)
        ) == 2
        current_task = connection.execute(
            select(TaskRow).where(TaskRow.task_id == submission.task.task_id)
        ).one()
        assert current_task.error_code == "TOOL_UNAVAILABLE"
