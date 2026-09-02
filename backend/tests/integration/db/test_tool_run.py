from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select
from sqlalchemy.engine import Engine

from materialsagent.domain.models.actor import Actor
from materialsagent.domain.models.conversation import Conversation
from materialsagent.domain.models.llm_call import LLMCall
from materialsagent.domain.models.task import Task
from materialsagent.domain.models.task_input_revision import TaskInputRevision
from materialsagent.domain.models.tool_run import ToolRun
from materialsagent.domain.ports.tool_registry import ExecutionPolicy
from materialsagent.domain.ports.unit_of_work import PersistenceConflictError
from materialsagent.infrastructure.db.session import create_session_factory
from materialsagent.infrastructure.db.tool_run import ToolRunRow
from materialsagent.infrastructure.db.unit_of_work import SQLAlchemyUnitOfWork


BASE = datetime(2026, 7, 20, 8, 0, tzinfo=timezone.utc)
SCHEMA_HASH = "f821240f782ce788bc723fd1acd02a2e58cedbf68b70b1414e2accd16d989d07"


def _seed_sources(engine: Engine) -> SQLAlchemyUnitOfWork:
    factory = lambda: SQLAlchemyUnitOfWork(create_session_factory(engine))
    with factory() as unit_of_work:
        unit_of_work.actors.add(Actor.local_anonymous("actor_1", created_at=BASE))
        unit_of_work.conversations.add(
            Conversation(
                conversation_id="conversation_1",
                actor_id="actor_1",
                title=None,
                created_at=BASE,
                updated_at=BASE,
            )
        )
        unit_of_work.tasks.add(
            Task(
                task_id="task_1",
                conversation_id="conversation_1",
                actor_id="actor_1",
                task_type="TOOL_EXECUTION",
                current_status="FAILED",
                selected_tool_run_id=None,
                selected_result_id=None,
                created_at=BASE,
                started_at=BASE,
                updated_at=BASE + timedelta(seconds=1),
                completed_at=BASE + timedelta(seconds=1),
                error_code="TOOL_UNAVAILABLE",
                safe_error_message="Tool execution is not available yet.",
                tool_id="zta35g_sem_virtual_lab",
                bound_tool_version="1",
                bound_schema_hash=SCHEMA_HASH,
            )
        )
        unit_of_work.llm_calls.add(
            LLMCall(
                llm_call_id="llm_1",
                task_id="task_1",
                conversation_id="conversation_1",
                request_id="message_request_1",
                purpose="CHAT_ORCHESTRATION",
                input_result_id=None,
                provider="mock",
                model_name="mock-chat",
                prompt_template_id=None,
                prompt_template_version=None,
                prompt_digest=None,
                generation_parameters={"temperature": 0, "max_tokens": 256},
                structured_output_summary={
                    "route": "TOOL_EXECUTION",
                    "tool_id": "zta35g_sem_virtual_lab",
                },
                usage={"input_tokens": 1, "output_tokens": 1},
                provider_request_id=None,
                status="SUCCEEDED",
                created_at=BASE,
                started_at=BASE,
                completed_at=BASE + timedelta(milliseconds=100),
                duration_ms=100,
                error_code=None,
                safe_error_message=None,
            )
        )
        unit_of_work.task_input_revisions.add(
            TaskInputRevision(
                task_input_revision_id="revision_1",
                task_id="task_1",
                request_id="message_request_1",
                source_llm_call_id="llm_1",
                source_message_ids=["message_1"],
                revision=1,
                raw_input={"material": "ZTA35G"},
                normalized_input={"material": "ZTA35G"},
                missing_fields=[],
                ambiguous_fields=[],
                validation_errors=[],
                created_at=BASE,
            )
        )
        unit_of_work.commit()
    return factory()


def _pending(tool_run_id: str = "tool_run_1", attempt_no: int = 1) -> ToolRun:
    return ToolRun.pending(
        tool_run_id=tool_run_id,
        task_id="task_1",
        request_id="execute_request_1",
        task_input_revision_id="revision_1",
        attempt_no=attempt_no,
        tool_id="zta35g_sem_virtual_lab",
        tool_version="1",
        schema_hash=SCHEMA_HASH,
        normalized_input_snapshot={"material": "ZTA35G"},
        execution_policy_snapshot=ExecutionPolicy.ANY_TASK,
        input_revision_no=1,
        requested_outputs=["sem_image", "mechanical_properties"],
        execution_input={
            "process_parameters": {
                "solution_temperature": 1000,
                "solution_time": 2.5,
                "aging_temperature": 730,
                "aging_time": 3.0,
            },
            "requested_outputs": ["sem_image", "mechanical_properties"],
            "runtime_parameters": {
                "seed": 101,
                "num_samples": 1,
                "guide_scale": 2.0,
                "timesteps": 1000,
            },
        },
        created_at=BASE + timedelta(seconds=2),
    )


def test_tool_run_round_trip_and_owned_query(
    migrated_database_engine: Engine,
) -> None:
    unit_of_work = _seed_sources(migrated_database_engine)
    with unit_of_work:
        unit_of_work.tool_runs.add(_pending())
        unit_of_work.commit()
    with SQLAlchemyUnitOfWork(
        create_session_factory(migrated_database_engine)
    ) as query:
        run = query.tool_runs.get_owned("tool_run_1", "actor_1")
        assert run == _pending()
        assert query.tool_runs.get_owned("tool_run_1", "actor_other") is None
        running = run.start(started_at=BASE + timedelta(seconds=3))
        updated = query.tool_runs.update(running, expected_status="PENDING")
        assert updated == running
        assert query.tool_runs.update(running, expected_status="PENDING") is None
        query.commit()


def test_task_attempt_is_unique_and_revision_fk_is_enforced(
    migrated_database_engine: Engine,
) -> None:
    unit_of_work = _seed_sources(migrated_database_engine)
    with unit_of_work:
        unit_of_work.tool_runs.add(_pending())
        unit_of_work.commit()

    with pytest.raises(PersistenceConflictError):
        with SQLAlchemyUnitOfWork(
            create_session_factory(migrated_database_engine)
        ) as duplicate:
            duplicate.tool_runs.add(_pending("tool_run_2", attempt_no=1))
            duplicate.commit()

    with migrated_database_engine.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(ToolRunRow)) == 1


def test_failure_terminal_fields_are_persisted_without_task_selection(
    migrated_database_engine: Engine,
) -> None:
    unit_of_work = _seed_sources(migrated_database_engine)
    pending = _pending()
    running = pending.start(started_at=BASE + timedelta(seconds=3))
    failed = running.fail(
        failed_at=BASE + timedelta(seconds=4),
        error_code="RUNTIME_TIMEOUT",
        safe_error_message="Tool Runtime timed out.",
    )
    with unit_of_work:
        unit_of_work.tool_runs.add(pending)
        unit_of_work.commit()
    with SQLAlchemyUnitOfWork(
        create_session_factory(migrated_database_engine)
    ) as transition:
        assert transition.tool_runs.update(
            running,
            expected_status="PENDING",
        ) == running
        transition.commit()
    with SQLAlchemyUnitOfWork(
        create_session_factory(migrated_database_engine)
    ) as transition:
        assert transition.tool_runs.update(
            failed,
            expected_status="RUNNING",
        ) == failed
        transition.commit()
    with SQLAlchemyUnitOfWork(
        create_session_factory(migrated_database_engine)
    ) as query:
        stored = query.tool_runs.get("tool_run_1")
        assert stored == failed
        task = query.tasks.get("task_1")
        assert task.selected_tool_run_id is None
        assert task.selected_result_id is None


def test_repository_only_inserts_pending_tool_runs(
    migrated_database_engine: Engine,
) -> None:
    unit_of_work = _seed_sources(migrated_database_engine)
    terminal = _pending().start(
        started_at=BASE + timedelta(seconds=3)
    ).fail(
        failed_at=BASE + timedelta(seconds=4),
        error_code="RUNTIME_TIMEOUT",
        safe_error_message="Tool Runtime timed out.",
    )

    with pytest.raises(ValueError, match="PENDING"):
        with unit_of_work:
            unit_of_work.tool_runs.add(terminal)

    with migrated_database_engine.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(ToolRunRow)) == 0


def test_failed_second_attempt_does_not_overwrite_successful_first_attempt(
    migrated_database_engine: Engine,
) -> None:
    unit_of_work = _seed_sources(migrated_database_engine)
    first_pending = _pending("tool_run_1", attempt_no=1)
    first_running = first_pending.start(started_at=BASE + timedelta(seconds=3))
    first_succeeded = first_running.complete_from_result(
        completed_outputs=["sem_image", "mechanical_properties"],
        failed_outputs=[],
        completed_at=BASE + timedelta(seconds=4),
        error_code=None,
        safe_error_message=None,
    )
    second_pending = _pending("tool_run_2", attempt_no=2)
    second_running = second_pending.start(started_at=BASE + timedelta(seconds=5))
    second_failed = second_running.fail(
        failed_at=BASE + timedelta(seconds=6),
        error_code="RUNTIME_TIMEOUT",
        safe_error_message="Tool Runtime timed out.",
    )

    with unit_of_work:
        unit_of_work.tool_runs.add(first_pending)
        unit_of_work.commit()
    with SQLAlchemyUnitOfWork(create_session_factory(migrated_database_engine)) as uow:
        assert uow.tool_runs.update(first_running, expected_status="PENDING") == first_running
        uow.commit()
    with SQLAlchemyUnitOfWork(create_session_factory(migrated_database_engine)) as uow:
        assert uow.tool_runs.update(first_succeeded, expected_status="RUNNING") == first_succeeded
        uow.commit()
    with SQLAlchemyUnitOfWork(create_session_factory(migrated_database_engine)) as uow:
        uow.tool_runs.add(second_pending)
        uow.commit()
    with SQLAlchemyUnitOfWork(create_session_factory(migrated_database_engine)) as uow:
        assert uow.tool_runs.update(second_running, expected_status="PENDING") == second_running
        uow.commit()
    with SQLAlchemyUnitOfWork(create_session_factory(migrated_database_engine)) as uow:
        assert uow.tool_runs.update(second_failed, expected_status="RUNNING") == second_failed
        uow.commit()

    with SQLAlchemyUnitOfWork(
        create_session_factory(migrated_database_engine)
    ) as query:
        assert query.tool_runs.get("tool_run_1") == first_succeeded
        assert query.tool_runs.get("tool_run_2") == second_failed
