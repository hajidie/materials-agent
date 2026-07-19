from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import delete, func, select, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError

from materialsagent.domain.models.actor import Actor
from materialsagent.domain.models.conversation import Conversation
from materialsagent.domain.models.llm_call import LLMCall
from materialsagent.domain.models.message import Message
from materialsagent.domain.models.task import Task
from materialsagent.domain.models.task_input_revision import TaskInputRevision
from materialsagent.domain.ports.unit_of_work import PersistenceConflictError
from materialsagent.infrastructure.db.session import create_session_factory


BASE_TIME = datetime(2026, 7, 19, 12, 0, tzinfo=timezone.utc)


def _opaque(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex}"


def _factory(engine: Engine) -> Any:
    from materialsagent.infrastructure.db.unit_of_work import (
        SQLAlchemyUnitOfWork,
    )

    return lambda: SQLAlchemyUnitOfWork(create_session_factory(engine))


def _parents(engine: Engine) -> tuple[Conversation, Task]:
    actor_id = _opaque("actor")
    conversation = Conversation(
        conversation_id=_opaque("conversation"),
        actor_id=actor_id,
        title=None,
        created_at=BASE_TIME,
        updated_at=BASE_TIME,
    )
    task = Task.pending(
        task_id=_opaque("task"),
        conversation_id=conversation.conversation_id,
        actor_id=actor_id,
        created_at=BASE_TIME,
    )
    with _factory(engine)() as unit_of_work:
        unit_of_work.actors.add(
            Actor.local_anonymous(actor_id, created_at=BASE_TIME)
        )
        unit_of_work.conversations.add(conversation)
        unit_of_work.tasks.add(task)
        unit_of_work.commit()
    return conversation, task


def _call(
    conversation: Conversation,
    task: Task,
    *,
    llm_call_id: str | None = None,
    request_id: str | None = None,
    status: str = "PENDING",
) -> LLMCall:
    started_at = None
    completed_at = None
    duration_ms = None
    error_code = None
    safe_error_message = None
    if status in {"RUNNING", "SUCCEEDED", "FAILED"}:
        started_at = BASE_TIME + timedelta(seconds=1)
    if status in {"SUCCEEDED", "FAILED"}:
        completed_at = BASE_TIME + timedelta(seconds=2)
        duration_ms = 1000
    if status == "FAILED":
        error_code = "LLM_PROVIDER_FAILED"
        safe_error_message = "LLM call failed."
    return LLMCall(
        llm_call_id=llm_call_id or _opaque("llm_call"),
        task_id=task.task_id,
        conversation_id=conversation.conversation_id,
        request_id=request_id or _opaque("request"),
        purpose="CHAT_ORCHESTRATION",
        input_result_id=None,
        provider="mock",
        model_name="mock-chat-orchestration-v1",
        prompt_template_id="chat-orchestration",
        prompt_template_version="1",
        prompt_digest="a" * 64,
        generation_parameters={"temperature": 0, "max_tokens": 256},
        structured_output_summary={
            "route": "TOOL_EXECUTION",
            "tool_id": "zta35g_sem_virtual_lab",
        },
        usage={"input_tokens": 10, "output_tokens": 5},
        provider_request_id=None,
        status=status,
        created_at=BASE_TIME,
        started_at=started_at,
        completed_at=completed_at,
        duration_ms=duration_ms,
        error_code=error_code,
        safe_error_message=safe_error_message,
    )


def test_repository_round_trip_and_task_request_queries_preserve_jsonb(
    migrated_database_engine: Engine,
) -> None:
    conversation, task = _parents(migrated_database_engine)
    first = _call(conversation, task, request_id="request_shared")
    second = _call(
        conversation,
        task,
        request_id="request_other",
        status="FAILED",
    )
    second = replace(
        second,
        created_at=BASE_TIME + timedelta(milliseconds=500),
    )
    factory = _factory(migrated_database_engine)

    with factory() as unit_of_work:
        unit_of_work.llm_calls.add(second)
        unit_of_work.llm_calls.add(first)
        unit_of_work.commit()

    with factory() as unit_of_work:
        assert unit_of_work.llm_calls.get(first.llm_call_id) == first
        assert unit_of_work.llm_calls.list_for_task(task.task_id) == [
            first,
            second,
        ]
        assert unit_of_work.llm_calls.list_for_task(
            task.task_id,
            request_id="request_shared",
        ) == [first]
        loaded = unit_of_work.llm_calls.get(first.llm_call_id)

    assert loaded is not None
    assert loaded.generation_parameters == {
        "temperature": 0,
        "max_tokens": 256,
    }
    assert loaded.structured_output_summary == {
        "route": "TOOL_EXECUTION",
        "tool_id": "zta35g_sem_virtual_lab",
    }
    assert loaded.usage == {"input_tokens": 10, "output_tokens": 5}


def test_conditional_status_updates_do_not_reopen_terminal_calls(
    migrated_database_engine: Engine,
) -> None:
    conversation, task = _parents(migrated_database_engine)
    pending = _call(conversation, task)
    running = replace(
        pending,
        status="RUNNING",
        started_at=BASE_TIME + timedelta(seconds=1),
    )
    succeeded = replace(
        running,
        status="SUCCEEDED",
        completed_at=BASE_TIME + timedelta(seconds=2),
        duration_ms=1000,
        structured_output_summary={"route": "KNOWLEDGE_ANSWER"},
    )
    factory = _factory(migrated_database_engine)

    with factory() as unit_of_work:
        unit_of_work.llm_calls.add(pending)
        unit_of_work.commit()
    with factory() as unit_of_work:
        assert unit_of_work.llm_calls.update(
            running,
            expected_status="PENDING",
        ) == running
        unit_of_work.commit()
    with factory() as unit_of_work:
        assert unit_of_work.llm_calls.update(
            succeeded,
            expected_status="RUNNING",
        ) == succeeded
        unit_of_work.commit()
    with factory() as unit_of_work:
        assert unit_of_work.llm_calls.update(
            running,
            expected_status="SUCCEEDED",
        ) is None
        unit_of_work.commit()
        assert unit_of_work.llm_calls.get(pending.llm_call_id) == succeeded


def test_uow_exception_and_uncommitted_exit_leave_no_llm_call(
    migrated_database_engine: Engine,
) -> None:
    from materialsagent.infrastructure.db.llm_call import LLMCallRow

    conversation, task = _parents(migrated_database_engine)
    first = _call(conversation, task)
    second = _call(conversation, task)
    factory = _factory(migrated_database_engine)

    with pytest.raises(RuntimeError, match="forced failure"):
        with factory() as unit_of_work:
            unit_of_work.llm_calls.add(first)
            raise RuntimeError("forced failure")
    with factory() as unit_of_work:
        unit_of_work.llm_calls.add(second)

    with migrated_database_engine.connect() as connection:
        assert connection.scalar(
            select(func.count()).select_from(LLMCallRow)
        ) == 0


@pytest.mark.parametrize("invalid_parent", ["task", "conversation"])
def test_task_and_conversation_foreign_keys_are_restrictive(
    migrated_database_engine: Engine,
    invalid_parent: str,
) -> None:
    conversation, task = _parents(migrated_database_engine)
    call = _call(conversation, task)
    if invalid_parent == "task":
        call = replace(call, task_id="missing_task")
    else:
        call = replace(call, conversation_id="missing_conversation")

    with pytest.raises(PersistenceConflictError):
        with _factory(migrated_database_engine)() as unit_of_work:
            unit_of_work.llm_calls.add(call)
            unit_of_work.commit()


def test_message_and_revision_llm_foreign_keys_unique_and_restrict_delete(
    migrated_database_engine: Engine,
) -> None:
    from materialsagent.infrastructure.db.llm_call import LLMCallRow

    conversation, task = _parents(migrated_database_engine)
    call = _call(conversation, task)
    user_message = Message.user(
        message_id=_opaque("message"),
        conversation_id=conversation.conversation_id,
        task_id=task.task_id,
        actor_id=conversation.actor_id,
        request_id=call.request_id,
        content_text="用户输入。",
        created_at=BASE_TIME,
    )
    assistant_message = Message(
        message_id=_opaque("message"),
        conversation_id=conversation.conversation_id,
        task_id=task.task_id,
        actor_id=conversation.actor_id,
        request_id=call.request_id,
        role="ASSISTANT",
        generation_source="LLM",
        content_text="安全答案。",
        structured_content=None,
        llm_call_id=call.llm_call_id,
        created_at=BASE_TIME,
    )
    revision = TaskInputRevision(
        task_input_revision_id=_opaque("revision"),
        task_id=task.task_id,
        request_id=call.request_id,
        source_llm_call_id=call.llm_call_id,
        source_message_ids=[user_message.message_id],
        revision=1,
        raw_input={"material": "ZTA35G"},
        normalized_input=None,
        missing_fields=["solution_time"],
        ambiguous_fields=[],
        validation_errors=[],
        created_at=BASE_TIME,
    )
    factory = _factory(migrated_database_engine)

    with factory() as unit_of_work:
        unit_of_work.llm_calls.add(call)
        unit_of_work.messages.add(user_message)
        unit_of_work.messages.add(assistant_message)
        unit_of_work.task_input_revisions.add(revision)
        unit_of_work.commit()

    duplicate = replace(
        assistant_message,
        message_id=_opaque("message"),
    )
    with pytest.raises(PersistenceConflictError):
        with factory() as unit_of_work:
            unit_of_work.messages.add(duplicate)
            unit_of_work.commit()

    with migrated_database_engine.begin() as connection:
        with pytest.raises(IntegrityError):
            connection.execute(
                delete(LLMCallRow).where(
                    LLMCallRow.llm_call_id == call.llm_call_id
                )
            )


@pytest.mark.parametrize(
    ("overrides", "constraint_name"),
    [
        ({"status": "UNKNOWN"}, "ck_llm_call_status_allowed"),
        ({"duration_ms": -1}, "ck_llm_call_duration_nonnegative"),
        (
            {"completed_at": BASE_TIME, "started_at": BASE_TIME + timedelta(seconds=1)},
            "ck_llm_call_completed_not_before_started",
        ),
        (
            {
                "status": "SUCCEEDED",
                "completed_at": BASE_TIME + timedelta(seconds=2),
                "duration_ms": 1000,
                "error_code": "STALE",
            },
            "ck_llm_call_succeeded_without_error",
        ),
        (
            {
                "status": "FAILED",
                "completed_at": BASE_TIME + timedelta(seconds=2),
                "duration_ms": 1000,
                "error_code": None,
            },
            "ck_llm_call_failed_requires_error",
        ),
    ],
)
def test_database_enforces_status_and_time_checks(
    migrated_database_engine: Engine,
    overrides: dict[str, object],
    constraint_name: str,
) -> None:
    conversation, task = _parents(migrated_database_engine)
    values = {
        "llm_call_id": _opaque("llm_call"),
        "task_id": task.task_id,
        "conversation_id": conversation.conversation_id,
        "request_id": _opaque("request"),
        "purpose": "CHAT_ORCHESTRATION",
        "input_result_id": None,
        "provider": "mock",
        "model_name": "mock-chat-orchestration-v1",
        "prompt_template_id": None,
        "prompt_template_version": None,
        "prompt_digest": None,
        "generation_parameters": "{}",
        "structured_output_summary": None,
        "usage": None,
        "provider_request_id": None,
        "status": "RUNNING",
        "created_at": BASE_TIME,
        "started_at": BASE_TIME + timedelta(seconds=1),
        "completed_at": None,
        "duration_ms": None,
        "error_code": None,
        "safe_error_message": None,
    }
    values.update(overrides)
    columns = ", ".join(values)
    parameters = ", ".join(f":{name}" for name in values)

    with migrated_database_engine.connect() as connection:
        transaction = connection.begin()
        try:
            with pytest.raises(IntegrityError, match=constraint_name):
                connection.execute(
                    text(f"INSERT INTO llm_call ({columns}) VALUES ({parameters})"),
                    values,
                )
        finally:
            transaction.rollback()


def test_missing_llm_foreign_keys_are_rejected_for_message_and_revision(
    migrated_database_engine: Engine,
) -> None:
    conversation, task = _parents(migrated_database_engine)
    factory = _factory(migrated_database_engine)
    missing_llm_id = "missing_llm_call"
    message = Message(
        message_id=_opaque("message"),
        conversation_id=conversation.conversation_id,
        task_id=task.task_id,
        actor_id=conversation.actor_id,
        request_id=_opaque("request"),
        role="ASSISTANT",
        generation_source="LLM",
        content_text="安全答案。",
        structured_content=None,
        llm_call_id=missing_llm_id,
        created_at=BASE_TIME,
    )
    user_message = Message.user(
        message_id=_opaque("message"),
        conversation_id=conversation.conversation_id,
        task_id=task.task_id,
        actor_id=conversation.actor_id,
        request_id=_opaque("request"),
        content_text="用户输入。",
        created_at=BASE_TIME,
    )
    revision = TaskInputRevision(
        task_input_revision_id=_opaque("revision"),
        task_id=task.task_id,
        request_id=_opaque("request"),
        source_llm_call_id=missing_llm_id,
        source_message_ids=[user_message.message_id],
        revision=1,
        raw_input={},
        normalized_input=None,
        missing_fields=[],
        ambiguous_fields=[],
        validation_errors=[],
        created_at=BASE_TIME,
    )

    for add_invalid in (
        lambda unit_of_work: unit_of_work.messages.add(message),
        lambda unit_of_work: (
            unit_of_work.messages.add(user_message),
            unit_of_work.task_input_revisions.add(revision),
        ),
    ):
        with pytest.raises(PersistenceConflictError):
            with factory() as unit_of_work:
                add_invalid(unit_of_work)
                unit_of_work.commit()
