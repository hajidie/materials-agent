from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.engine import Engine

from materialsagent.domain.models.actor import Actor
from materialsagent.domain.ports.unit_of_work import PersistenceConflictError
from materialsagent.infrastructure.db.actor import ActorRow
from materialsagent.infrastructure.db.session import create_session_factory


BASE_TIME = datetime(2026, 7, 19, 0, 0, tzinfo=timezone.utc)


def _domain_types() -> tuple[type[Any], type[Any], type[Any], type[Any]]:
    from materialsagent.domain.models.conversation import Conversation
    from materialsagent.domain.models.message import Message
    from materialsagent.domain.models.task import Task
    from materialsagent.domain.models.task_input_revision import (
        TaskInputRevision,
    )

    return Conversation, Message, Task, TaskInputRevision


def _uow_factory(engine: Engine) -> Any:
    from materialsagent.infrastructure.db.unit_of_work import (
        SQLAlchemyUnitOfWork,
    )

    session_factory = create_session_factory(engine)
    return lambda: SQLAlchemyUnitOfWork(session_factory)


def _opaque(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex}"


def _persist_actor(engine: Engine, actor_id: str) -> None:
    factory = _uow_factory(engine)
    with factory() as unit_of_work:
        unit_of_work.actors.add(
            Actor.local_anonymous(actor_id, created_at=BASE_TIME)
        )
        unit_of_work.commit()


def _valid_facts(actor_id: str | None = None) -> tuple[Any, Any, Any, Any]:
    Conversation, Message, Task, TaskInputRevision = _domain_types()
    owner_id = actor_id or _opaque("actor")
    conversation_id = _opaque("conversation")
    task_id = _opaque("task")
    message_id = _opaque("message")
    request_id = _opaque("request")
    conversation = Conversation(
        conversation_id=conversation_id,
        actor_id=owner_id,
        title=None,
        created_at=BASE_TIME,
        updated_at=BASE_TIME,
    )
    task = Task.pending(
        task_id=task_id,
        conversation_id=conversation_id,
        actor_id=owner_id,
        created_at=BASE_TIME,
    )
    message = Message.user(
        message_id=message_id,
        conversation_id=conversation_id,
        task_id=task_id,
        actor_id=owner_id,
        request_id=request_id,
        content_text="生成一张材料显微组织图。",
        created_at=BASE_TIME,
    )
    revision = TaskInputRevision(
        task_input_revision_id=_opaque("revision"),
        task_id=task_id,
        request_id=request_id,
        source_llm_call_id=None,
        source_message_ids=[message_id],
        revision=1,
        raw_input={"prompt": "生成一张材料显微组织图。"},
        normalized_input=None,
        missing_fields=["solution_temperature"],
        ambiguous_fields=[],
        validation_errors=[],
        created_at=BASE_TIME,
    )
    return conversation, task, message, revision


@pytest.mark.parametrize(
    ("fact_index", "field_name"),
    [
        (0, "conversation_id"),
        (0, "actor_id"),
        (1, "task_id"),
        (1, "conversation_id"),
        (1, "actor_id"),
        (2, "message_id"),
        (2, "conversation_id"),
        (2, "task_id"),
        (2, "actor_id"),
        (2, "request_id"),
        (3, "task_input_revision_id"),
        (3, "task_id"),
        (3, "request_id"),
    ],
)
def test_domain_rejects_blank_required_ids(
    fact_index: int,
    field_name: str,
) -> None:
    fact = _valid_facts()[fact_index]

    with pytest.raises(ValueError, match=field_name):
        replace(fact, **{field_name: "   "})


def test_user_message_factory_rejects_blank_content() -> None:
    _, Message, _, _ = _domain_types()

    with pytest.raises(ValueError, match="content_text"):
        Message.user(
            message_id=_opaque("message"),
            conversation_id=_opaque("conversation"),
            task_id=_opaque("task"),
            actor_id=_opaque("actor"),
            request_id=_opaque("request"),
            content_text=" \t ",
            created_at=BASE_TIME,
        )


def test_domain_rejects_non_utc_times() -> None:
    conversation = _valid_facts()[0]

    with pytest.raises(ValueError, match="created_at"):
        replace(conversation, created_at=BASE_TIME.replace(tzinfo=None))

    with pytest.raises(ValueError, match="updated_at"):
        replace(
            conversation,
            updated_at=datetime(
                2026,
                7,
                19,
                tzinfo=timezone(timedelta(hours=8)),
            ),
        )


@pytest.mark.parametrize(
    ("role", "generation_source", "llm_call_id"),
    [
        ("SYSTEM", "USER", None),
        ("USER", "LLM", _opaque("llm")),
        ("USER", "USER", _opaque("llm")),
        ("ASSISTANT", "LLM", None),
        ("USER", "TEMPLATE", None),
    ],
)
def test_message_rejects_illegal_role_source_combinations(
    role: str,
    generation_source: str,
    llm_call_id: str | None,
) -> None:
    message = _valid_facts()[2]

    with pytest.raises(ValueError):
        replace(
            message,
            role=role,
            generation_source=generation_source,
            llm_call_id=llm_call_id,
        )


def test_task_rejects_unknown_status() -> None:
    task = _valid_facts()[1]

    with pytest.raises(ValueError, match="current_status"):
        replace(task, current_status="CANCELLED")


def test_task_rejects_selected_result_without_selected_run() -> None:
    task = _valid_facts()[1]

    with pytest.raises(ValueError, match="selected_tool_run_id"):
        replace(task, selected_result_id=_opaque("result"))


def test_task_rejects_started_at_earlier_than_created_at() -> None:
    task = _valid_facts()[1]

    with pytest.raises(
        ValueError,
        match="started_at must not be earlier than created_at",
    ):
        replace(
            task,
            created_at=BASE_TIME.replace(hour=10),
            started_at=BASE_TIME.replace(hour=9, minute=59),
            updated_at=BASE_TIME.replace(hour=10),
        )


def test_task_rejects_completed_at_earlier_than_started_at() -> None:
    task = _valid_facts()[1]

    with pytest.raises(
        ValueError,
        match="completed_at must not be earlier than started_at",
    ):
        replace(
            task,
            created_at=BASE_TIME.replace(hour=10),
            started_at=BASE_TIME.replace(hour=11),
            updated_at=BASE_TIME.replace(hour=11),
            completed_at=BASE_TIME.replace(hour=10, minute=30),
        )


def test_revision_rejects_zero_revision() -> None:
    revision = _valid_facts()[3]

    with pytest.raises(ValueError, match="revision"):
        replace(revision, revision=0)


def test_revision_rejects_empty_source_message_ids() -> None:
    revision = _valid_facts()[3]

    with pytest.raises(ValueError, match="source_message_ids"):
        replace(revision, source_message_ids=[])


def test_repository_round_trip_preserves_conversation_task_message(
    migrated_database_engine: Engine,
) -> None:
    actor_id = _opaque("actor")
    _persist_actor(migrated_database_engine, actor_id)
    conversation, task, message, _ = _valid_facts(actor_id)
    factory = _uow_factory(migrated_database_engine)

    with factory() as unit_of_work:
        unit_of_work.conversations.add(conversation)
        unit_of_work.tasks.add(task)
        unit_of_work.messages.add(message)
        unit_of_work.commit()

    with factory() as unit_of_work:
        assert unit_of_work.conversations.get(
            conversation.conversation_id
        ) == conversation
        assert unit_of_work.tasks.get(task.task_id) == task
        assert unit_of_work.messages.get(message.message_id) == message
    from materialsagent.infrastructure.db.conversation_task import MessageRow

    with migrated_database_engine.connect() as connection:
        assert connection.scalar(
            select(MessageRow.structured_content.is_(None)).where(
                MessageRow.message_id == message.message_id
            )
        ) is True


def test_revision_repository_lists_in_stable_ascending_order(
    migrated_database_engine: Engine,
) -> None:
    actor_id = _opaque("actor")
    _persist_actor(migrated_database_engine, actor_id)
    conversation, task, message, first = _valid_facts(actor_id)
    second = replace(
        first,
        task_input_revision_id=_opaque("revision"),
        source_message_ids=[message.message_id, _opaque("message")],
        revision=2,
        raw_input={"attempt": 2},
        created_at=BASE_TIME + timedelta(seconds=1),
    )
    factory = _uow_factory(migrated_database_engine)

    with factory() as unit_of_work:
        unit_of_work.conversations.add(conversation)
        unit_of_work.tasks.add(task)
        unit_of_work.messages.add(message)
        unit_of_work.task_input_revisions.add(second)
        unit_of_work.task_input_revisions.add(first)
        unit_of_work.commit()

    with factory() as unit_of_work:
        loaded = unit_of_work.task_input_revisions.list_for_task(task.task_id)

    assert loaded == [first, second]
    from materialsagent.infrastructure.db.conversation_task import (
        TaskInputRevisionRow,
    )

    with migrated_database_engine.connect() as connection:
        assert connection.scalar(
            select(TaskInputRevisionRow.normalized_input.is_(None)).where(
                TaskInputRevisionRow.task_input_revision_id
                == first.task_input_revision_id
            )
        ) is True


def test_owned_queries_filter_other_actors_and_sort_conversations(
    migrated_database_engine: Engine,
) -> None:
    Conversation, _, Task, _ = _domain_types()
    actor_a = _opaque("actor_a")
    actor_b = _opaque("actor_b")
    _persist_actor(migrated_database_engine, actor_a)
    _persist_actor(migrated_database_engine, actor_b)
    shared_updated_at = BASE_TIME + timedelta(minutes=5)
    conversation_low = Conversation(
        conversation_id="conversation_a_low",
        actor_id=actor_a,
        title=None,
        created_at=BASE_TIME,
        updated_at=shared_updated_at,
    )
    conversation_high = replace(
        conversation_low,
        conversation_id="conversation_a_high",
    )
    conversation_b = Conversation(
        conversation_id=_opaque("conversation_b"),
        actor_id=actor_b,
        title=None,
        created_at=BASE_TIME,
        updated_at=BASE_TIME + timedelta(minutes=10),
    )
    task_a = Task.pending(
        task_id=_opaque("task_a"),
        conversation_id=conversation_low.conversation_id,
        actor_id=actor_a,
        created_at=BASE_TIME,
    )
    factory = _uow_factory(migrated_database_engine)

    with factory() as unit_of_work:
        unit_of_work.conversations.add(conversation_low)
        unit_of_work.conversations.add(conversation_high)
        unit_of_work.conversations.add(conversation_b)
        unit_of_work.tasks.add(task_a)
        unit_of_work.commit()

    with factory() as unit_of_work:
        assert unit_of_work.conversations.get_owned(
            conversation_low.conversation_id,
            actor_b,
        ) is None
        assert unit_of_work.tasks.get_owned(task_a.task_id, actor_b) is None
        assert unit_of_work.conversations.list_owned(actor_b) == [conversation_b]
        assert unit_of_work.conversations.list_owned(actor_a) == [
            conversation_low,
            conversation_high,
        ]


def test_unit_of_work_rolls_back_all_three_facts_and_remains_usable(
    migrated_database_engine: Engine,
) -> None:
    from materialsagent.infrastructure.db.conversation_task import (
        ConversationRow,
        MessageRow,
        TaskRow,
    )

    actor_id = _opaque("actor")
    _persist_actor(migrated_database_engine, actor_id)
    conversation, task, message, _ = _valid_facts(actor_id)
    factory = _uow_factory(migrated_database_engine)
    failed_unit_of_work = None

    with pytest.raises(RuntimeError, match="forced failure"):
        with factory() as unit_of_work:
            failed_unit_of_work = unit_of_work
            unit_of_work.conversations.add(conversation)
            unit_of_work.tasks.add(task)
            unit_of_work.messages.add(message)
            raise RuntimeError("forced failure")

    assert failed_unit_of_work is not None
    assert failed_unit_of_work.session is None
    with migrated_database_engine.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(ActorRow)) == 1
        assert connection.scalar(
            select(func.count()).select_from(ConversationRow)
        ) == 0
        assert connection.scalar(select(func.count()).select_from(TaskRow)) == 0
        assert connection.scalar(
            select(func.count()).select_from(MessageRow)
        ) == 0

    survivor = replace(
        conversation,
        conversation_id=_opaque("surviving_conversation"),
    )
    with factory() as unit_of_work:
        unit_of_work.conversations.add(survivor)
        unit_of_work.commit()
    with factory() as unit_of_work:
        assert unit_of_work.conversations.get(
            survivor.conversation_id
        ) == survivor


def test_replayed_primary_keys_raise_safe_conflict_without_extra_facts(
    migrated_database_engine: Engine,
) -> None:
    from materialsagent.infrastructure.db.conversation_task import (
        ConversationRow,
        MessageRow,
        TaskRow,
    )

    actor_id = _opaque("actor")
    _persist_actor(migrated_database_engine, actor_id)
    conversation, task, message, _ = _valid_facts(actor_id)
    factory = _uow_factory(migrated_database_engine)

    with factory() as unit_of_work:
        unit_of_work.conversations.add(conversation)
        unit_of_work.tasks.add(task)
        unit_of_work.messages.add(message)
        unit_of_work.commit()

    for add_duplicate in (
        lambda uow: uow.conversations.add(conversation),
        lambda uow: uow.tasks.add(task),
        lambda uow: uow.messages.add(message),
    ):
        with pytest.raises(PersistenceConflictError) as exc_info:
            with factory() as unit_of_work:
                add_duplicate(unit_of_work)
                unit_of_work.commit()
        assert str(exc_info.value) == "Persistence conflict."

    with migrated_database_engine.connect() as connection:
        assert connection.scalar(
            select(func.count()).select_from(ConversationRow)
        ) == 1
        assert connection.scalar(select(func.count()).select_from(TaskRow)) == 1
        assert connection.scalar(
            select(func.count()).select_from(MessageRow)
        ) == 1


def test_database_defers_cross_conversation_message_task_check_to_application(
    migrated_database_engine: Engine,
) -> None:
    actor_id = _opaque("actor")
    _persist_actor(migrated_database_engine, actor_id)
    conversation_a, task_a, message, _ = _valid_facts(actor_id)
    conversation_b = replace(
        conversation_a,
        conversation_id=_opaque("conversation_b"),
    )
    cross_conversation_message = replace(
        message,
        conversation_id=conversation_b.conversation_id,
    )
    factory = _uow_factory(migrated_database_engine)

    with factory() as unit_of_work:
        unit_of_work.conversations.add(conversation_a)
        unit_of_work.conversations.add(conversation_b)
        unit_of_work.tasks.add(task_a)
        unit_of_work.messages.add(cross_conversation_message)
        unit_of_work.commit()

    with factory() as unit_of_work:
        assert unit_of_work.messages.get(
            cross_conversation_message.message_id
        ) == cross_conversation_message
