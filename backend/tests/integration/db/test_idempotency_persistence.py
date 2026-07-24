from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from uuid import uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.engine import Engine

from materialsagent.domain.models.actor import Actor
from materialsagent.domain.models.conversation import Conversation
from materialsagent.domain.models.idempotency_record import IdempotencyRecord
from materialsagent.domain.models.message import Message
from materialsagent.domain.models.task import Task
from materialsagent.domain.ports.unit_of_work import PersistenceConflictError
from materialsagent.infrastructure.db.idempotency_record import (
    IdempotencyRecordRow,
)
from materialsagent.infrastructure.db.session import create_session_factory
from materialsagent.infrastructure.db.unit_of_work import SQLAlchemyUnitOfWork


NOW = datetime(2026, 7, 24, tzinfo=timezone.utc)


def _uow(engine: Engine) -> SQLAlchemyUnitOfWork:
    return SQLAlchemyUnitOfWork(create_session_factory(engine))


def _seed_task(engine: Engine, suffix: str) -> tuple[str, str, str]:
    actor_id = f"actor_{suffix}"
    conversation_id = f"conv_{suffix}"
    task_id = f"task_{suffix}"
    message_id = f"msg_{suffix}"
    with _uow(engine) as unit_of_work:
        unit_of_work.actors.add(Actor.local_anonymous(actor_id, created_at=NOW))
        unit_of_work.conversations.add(
            Conversation(
                conversation_id=conversation_id,
                actor_id=actor_id,
                title=None,
                created_at=NOW,
                updated_at=NOW,
            )
        )
        unit_of_work.tasks.add(
            Task.pending(
                task_id=task_id,
                conversation_id=conversation_id,
                actor_id=actor_id,
                created_at=NOW,
            )
        )
        unit_of_work.messages.add(
            Message.user(
                message_id=message_id,
                conversation_id=conversation_id,
                task_id=task_id,
                actor_id=actor_id,
                request_id=f"source_req_{suffix}",
                content_text="完整合法",
                created_at=NOW,
            )
        )
        unit_of_work.commit()
    return actor_id, task_id, message_id


def _record(
    suffix: str,
    actor_id: str,
    task_id: str,
    message_id: str,
    *,
    key: str = "same-key",
    first_request_id: str | None = None,
) -> IdempotencyRecord:
    return IdempotencyRecord(
        idempotency_record_id=f"idem_{suffix}",
        actor_id=actor_id,
        operation="TASK_CREATE",
        idempotency_key=key,
        request_digest="b" * 64,
        first_request_id=first_request_id or f"req_{suffix}",
        task_id=task_id,
        message_id=message_id,
        task_input_revision_id=None,
        tool_run_id=None,
        explanation_id=None,
        created_at=NOW,
        expires_at=None,
    )


def test_idempotency_record_round_trip_and_actor_scope(
    migrated_database_engine: Engine,
) -> None:
    actor_a, task_a, message_a = _seed_task(migrated_database_engine, "a")
    actor_b, task_b, message_b = _seed_task(migrated_database_engine, "b")
    record_a = _record("a", actor_a, task_a, message_a)
    record_b = _record("b", actor_b, task_b, message_b)
    with _uow(migrated_database_engine) as unit_of_work:
        unit_of_work.idempotency_records.add(record_a)
        unit_of_work.idempotency_records.add(record_b)
        unit_of_work.commit()

    with _uow(migrated_database_engine) as unit_of_work:
        assert (
            unit_of_work.idempotency_records.get_by_scope(
                actor_a,
                "TASK_CREATE",
                "same-key",
            )
            == record_a
        )
        assert (
            unit_of_work.idempotency_records.get_by_first_request_id("req_b")
            == record_b
        )


def test_scope_key_and_first_request_are_database_unique(
    migrated_database_engine: Engine,
) -> None:
    actor_id, task_id, message_id = _seed_task(
        migrated_database_engine,
        "unique",
    )
    with _uow(migrated_database_engine) as unit_of_work:
        unit_of_work.idempotency_records.add(
            _record("first", actor_id, task_id, message_id)
        )
        unit_of_work.commit()

    with pytest.raises(PersistenceConflictError):
        with _uow(migrated_database_engine) as unit_of_work:
            unit_of_work.idempotency_records.add(
                _record(
                    "duplicate_scope",
                    actor_id,
                    task_id,
                    message_id,
                    first_request_id="req_other",
                )
            )
            unit_of_work.commit()
    with pytest.raises(PersistenceConflictError):
        with _uow(migrated_database_engine) as unit_of_work:
            unit_of_work.idempotency_records.add(
                _record(
                    "duplicate_request",
                    actor_id,
                    task_id,
                    message_id,
                    key="other-key",
                    first_request_id="req_first",
                )
            )
            unit_of_work.commit()


def test_twenty_concurrent_scope_inserts_have_one_winner(
    migrated_database_engine: Engine,
) -> None:
    actor_id, task_id, message_id = _seed_task(
        migrated_database_engine,
        "concurrent",
    )

    def insert_one(index: int) -> bool:
        try:
            with _uow(migrated_database_engine) as unit_of_work:
                unit_of_work.idempotency_records.add(
                    _record(
                        uuid4().hex,
                        actor_id,
                        task_id,
                        message_id,
                        first_request_id=f"req_concurrent_{index}",
                    )
                )
                unit_of_work.commit()
            return True
        except PersistenceConflictError:
            return False

    with ThreadPoolExecutor(max_workers=20) as executor:
        winners = list(executor.map(insert_one, range(20)))
    assert sum(winners) == 1
    with migrated_database_engine.connect() as connection:
        assert connection.scalar(
            select(func.count()).select_from(IdempotencyRecordRow)
        ) == 1
