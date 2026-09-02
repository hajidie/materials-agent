from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import event, func, select, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session
import pytest

from materialsagent.application.tasks import TaskQueryService
from materialsagent.application.context import ActorContext

from materialsagent.domain.models.actor import Actor
from materialsagent.domain.models.asset import Asset
from materialsagent.domain.models.conversation import Conversation
from materialsagent.domain.models.explanation import (
    NaturalLanguageExplanation,
)
from materialsagent.domain.models.llm_call import LLMCall
from materialsagent.domain.models.message import Message
from materialsagent.domain.models.result_asset_link import ResultAssetLink
from materialsagent.domain.models.task import Task
from materialsagent.domain.models.task_input_revision import TaskInputRevision
from materialsagent.domain.models.tool_result import ToolResult
from materialsagent.domain.models.tool_run import ToolRun
from materialsagent.domain.ports.unit_of_work import PersistenceError
from materialsagent.domain.ports.tool_registry import ExecutionPolicy
from materialsagent.infrastructure.db import timeline_query as timeline_query_db
from materialsagent.infrastructure.db.conversation_task import (
    MessageRow,
    TaskRow,
)
from materialsagent.infrastructure.db.session import create_session_factory
from materialsagent.infrastructure.db.timeline_query import (
    SQLAlchemyTimelineQueryRepository,
)
from materialsagent.infrastructure.db.unit_of_work import SQLAlchemyUnitOfWork


BASE = datetime(2026, 7, 24, 4, 0, tzinfo=timezone.utc)
SCHEMA_HASH = "f821240f782ce788bc723fd1acd02a2e58cedbf68b70b1414e2accd16d989d07"


def _assistant(
    message_id: str,
    task_id: str,
    *,
    created_at: datetime,
) -> Message:
    return Message(
        message_id=message_id,
        conversation_id="conversation_timeline",
        task_id=task_id,
        actor_id="actor_timeline",
        request_id=f"request_{message_id}",
        role="ASSISTANT",
        generation_source="TEMPLATE",
        content_text=message_id,
        structured_content=None,
        llm_call_id=None,
        created_at=created_at,
    )


def _seed_mixed_timeline(engine) -> None:
    knowledge = Task.pending(
        task_id="task_knowledge",
        conversation_id="conversation_timeline",
        actor_id="actor_timeline",
        created_at=BASE,
    )
    knowledge.task_type = "KNOWLEDGE_QA"
    tool = Task.pending(
        task_id="task_tool",
        conversation_id="conversation_timeline",
        actor_id="actor_timeline",
        created_at=BASE,
    )
    tool.task_type = "TOOL_EXECUTION"
    undecided = Task.pending(
        task_id="task_undecided",
        conversation_id="conversation_timeline",
        actor_id="actor_timeline",
        created_at=BASE + timedelta(minutes=1),
    )
    messages = (
        Message.user(
            message_id="message_knowledge_user",
            conversation_id="conversation_timeline",
            task_id=knowledge.task_id,
            actor_id="actor_timeline",
            request_id="request_knowledge",
            content_text="knowledge",
            created_at=BASE,
        ),
        _assistant(
            "message_knowledge_assistant",
            knowledge.task_id,
            created_at=BASE,
        ),
        Message.user(
            message_id="message_tool_initial",
            conversation_id="conversation_timeline",
            task_id=tool.task_id,
            actor_id="actor_timeline",
            request_id="request_tool",
            content_text="tool",
            created_at=BASE,
        ),
        _assistant(
            "message_tool_follow_up",
            tool.task_id,
            created_at=BASE + timedelta(seconds=1),
        ),
        Message.user(
            message_id="message_undecided",
            conversation_id="conversation_timeline",
            task_id=undecided.task_id,
            actor_id="actor_timeline",
            request_id="request_undecided",
            content_text="undecided",
            created_at=BASE + timedelta(minutes=1),
        ),
    )
    with SQLAlchemyUnitOfWork(
        create_session_factory(engine)
    ) as unit_of_work:
        unit_of_work.actors.add(
            Actor.local_anonymous("actor_timeline", created_at=BASE)
        )
        unit_of_work.conversations.add(
            Conversation(
                conversation_id="conversation_timeline",
                actor_id="actor_timeline",
                title="Timeline",
                created_at=BASE,
                updated_at=BASE,
            )
        )
        for task in (knowledge, tool, undecided):
            unit_of_work.tasks.add(task)
        for message in messages:
            unit_of_work.messages.add(message)
        unit_of_work.commit()


def _terminal_run(
    *,
    task_id: str,
    revision_id: str,
    attempt_no: int,
    status: str,
    created_at: datetime,
) -> ToolRun:
    requested = ["sem_image", "mechanical_properties"]
    completed = (
        requested
        if status == "SUCCEEDED"
        else ["sem_image"]
    )
    failed = [] if status == "SUCCEEDED" else ["mechanical_properties"]
    pending = ToolRun.pending(
        tool_run_id=f"{task_id}_run_{attempt_no:02}",
        task_id=task_id,
        request_id=f"{task_id}_run_request_{attempt_no:02}",
        task_input_revision_id=revision_id,
        attempt_no=attempt_no,
        tool_id="zta35g_sem_virtual_lab",
        tool_version="0.1.0",
        schema_hash=SCHEMA_HASH,
        normalized_input_snapshot={},
        execution_policy_snapshot=ExecutionPolicy.ANY_TASK,
        input_revision_no=1,
        execution_input={},
        requested_outputs=requested,
        created_at=created_at,
    )
    running = pending.start(started_at=created_at)
    recorded = running.record_runtime_output(
        actual_runtime_parameters={},
        diagnostics=[],
        output_summary={},
        model_bundle_id="test-bundle",
    )
    return recorded.complete_from_result(
        completed_outputs=completed,
        failed_outputs=failed,
        completed_at=created_at + timedelta(milliseconds=100),
        error_code=(
            None if status == "SUCCEEDED" else "PERFORMANCE_FAILED"
        ),
        safe_error_message=(
            None if status == "SUCCEEDED" else "性能预测失败。"
        ),
    )


def _result_for_run(
    run: ToolRun,
    *,
    result_id: str,
) -> ToolResult:
    return ToolResult(
        result_id=result_id,
        task_id=run.task_id,
        tool_run_id=run.tool_run_id,
        actor_id=f"{run.task_id}_actor",
        status=run.current_status,
        requested_outputs=list(run.requested_outputs),
        completed_outputs=list(run.completed_outputs),
        failed_outputs=list(run.failed_outputs),
        data=(
            {
                "yield_strength": {"value": 650.0, "unit": "MPa"},
                "elongation": {"value": 3.2, "unit": "%"},
            }
            if "mechanical_properties" in run.completed_outputs
            else {}
        ),
        warnings=[],
        provenance={
            "input_revision": 1,
            "normalized_process_parameters": {},
            "actual_runtime_parameters": {},
        },
        error=(
            None
            if run.current_status == "SUCCEEDED"
            else {
                "code": "PERFORMANCE_FAILED",
                "safe_message": "性能预测失败。",
                "retryable": True,
            }
        ),
        tool_id=run.tool_id,
        tool_version=run.tool_version,
        schema_hash=run.schema_hash,
        created_at=run.completed_at or run.created_at,
    )


def _available_asset(
    *,
    task_id: str,
    run_id: str,
    asset_id: str,
    created_at: datetime,
) -> Asset:
    return Asset.pending(
        asset_id=asset_id,
        task_id=task_id,
        producer_tool_run_id=run_id,
        actor_id=f"{task_id}_actor",
        operation_id=f"{asset_id}_operation",
        role="requested_output",
        object_key=f"assets/test/{asset_id}.png",
        pending_since=created_at,
        created_at=created_at,
    ).mark_available(
        sha256="a" * 64,
        size_bytes=123,
        width=512,
        height=512,
        bit_depth=8,
        media_type="image/png",
        encoding_rule="linear[-1,1]-half-up-uint8-png-l",
        available_at=created_at + timedelta(milliseconds=100),
    )


def _explanation_attempt(
    *,
    task_id: str,
    result_id: str,
    attempt_no: int,
    status: str,
    created_at: datetime,
) -> tuple[NaturalLanguageExplanation, LLMCall]:
    explanation_id = f"{task_id}_explanation_{attempt_no:02}"
    call_id = f"{task_id}_explanation_call_{attempt_no:02}"
    succeeded = status == "SUCCEEDED"
    completed_at = created_at + timedelta(milliseconds=100)
    return (
        NaturalLanguageExplanation(
            explanation_id=explanation_id,
            task_id=task_id,
            result_id=result_id,
            llm_call_id=call_id,
            attempt_no=attempt_no,
            status=status,
            language="zh-CN",
            text=(f"解释 {attempt_no}" if succeeded else None),
            created_at=created_at,
            started_at=created_at,
            completed_at=completed_at,
            duration_ms=100,
            error_code=(None if succeeded else "EXPLANATION_FAILED"),
            safe_error_message=(None if succeeded else "解释生成失败。"),
        ),
        LLMCall(
            llm_call_id=call_id,
            task_id=task_id,
            conversation_id=f"{task_id}_conversation",
            request_id=f"{task_id}_explanation_request_{attempt_no:02}",
            purpose="TOOL_RESULT_EXPLANATION",
            input_result_id=result_id,
            provider="mock",
            model_name="mock-model",
            prompt_template_id="explanation",
            prompt_template_version="1",
            prompt_digest="a" * 64,
            generation_parameters={"temperature": 0, "max_tokens": 512},
            structured_output_summary=None,
            usage=(
                {"input_tokens": 1, "output_tokens": 1}
                if succeeded
                else None
            ),
            provider_request_id=None,
            status=status,
            created_at=created_at,
            started_at=created_at,
            completed_at=completed_at,
            duration_ms=100,
            error_code=(None if succeeded else "EXPLANATION_FAILED"),
            safe_error_message=(None if succeeded else "解释生成失败。"),
        ),
    )


def _seed_task_detail(
    engine,
    *,
    task_id: str,
    attempt_count: int,
    asset_count: int,
    explanation_statuses: tuple[str, ...],
    selected_status: str = "SUCCEEDED",
    task_status: str = "SUCCEEDED",
) -> dict[str, str]:
    actor_id = f"{task_id}_actor"
    conversation_id = f"{task_id}_conversation"
    message_id = f"{task_id}_message"
    revision_id = f"{task_id}_revision"
    created_at = BASE + timedelta(hours=1)
    with SQLAlchemyUnitOfWork(
        create_session_factory(engine)
    ) as unit_of_work:
        unit_of_work.actors.add(
            Actor.local_anonymous(actor_id, created_at=created_at)
        )
        unit_of_work.conversations.add(
            Conversation(
                conversation_id=conversation_id,
                actor_id=actor_id,
                title="Task detail",
                created_at=created_at,
                updated_at=created_at,
            )
        )
        task = Task.pending(
            task_id=task_id,
            conversation_id=conversation_id,
            actor_id=actor_id,
            created_at=created_at,
        )
        task.task_type = "TOOL_EXECUTION"
        unit_of_work.tasks.add(task)
        unit_of_work.messages.add(
            Message.user(
                message_id=message_id,
                conversation_id=conversation_id,
                task_id=task_id,
                actor_id=actor_id,
                request_id=f"{task_id}_message_request",
                content_text="tool request",
                created_at=created_at,
            )
        )
        unit_of_work.task_input_revisions.add(
            TaskInputRevision(
                task_input_revision_id=revision_id,
                task_id=task_id,
                request_id=f"{task_id}_revision_request",
                source_llm_call_id=None,
                source_message_ids=[message_id],
                revision=1,
                raw_input={"material": "ZTA35G"},
                normalized_input={"material": "ZTA35G"},
                missing_fields=[],
                ambiguous_fields=[],
                validation_errors=[],
                created_at=created_at,
            )
        )
        unit_of_work.commit()

    selected_run: ToolRun | None = None
    result: ToolResult | None = None
    with SQLAlchemyUnitOfWork(
        create_session_factory(engine)
    ) as unit_of_work:
        for attempt_no in range(1, attempt_count + 1):
            selected = attempt_no == attempt_count
            run = _terminal_run(
                task_id=task_id,
                revision_id=revision_id,
                attempt_no=attempt_no,
                status=(selected_status if selected else "SUCCEEDED"),
                created_at=created_at + timedelta(seconds=attempt_no),
            )
            unit_of_work.tool_runs.add(run)
            if selected:
                selected_run = run
        assert selected_run is not None
        result = _result_for_run(
            selected_run,
            result_id=f"{task_id}_result",
        )
        unit_of_work.tool_results.add(result)
        for index in range(asset_count):
            asset = _available_asset(
                task_id=task_id,
                run_id=selected_run.tool_run_id,
                asset_id=f"{task_id}_asset_{index:02}",
                created_at=created_at + timedelta(minutes=1, seconds=index),
            )
            unit_of_work.assets.add(asset)
            unit_of_work.result_asset_links.add(
                ResultAssetLink(
                    result_id=result.result_id,
                    asset_id=asset.asset_id,
                    artifact_order=index,
                    created_at=asset.created_at,
                )
            )
        for attempt_no, status in enumerate(
            explanation_statuses,
            start=1,
        ):
            explanation, call = _explanation_attempt(
                task_id=task_id,
                result_id=result.result_id,
                attempt_no=attempt_no,
                status=status,
                created_at=created_at
                + timedelta(minutes=2, seconds=attempt_no),
            )
            unit_of_work.llm_calls.add(call)
            unit_of_work.explanations.add(explanation)
        unit_of_work.commit()

    assert result is not None
    assert selected_run is not None
    with engine.begin() as connection:
        connection.execute(
            update(TaskRow)
            .where(TaskRow.task_id == task_id)
            .values(
                current_status=task_status,
                selected_tool_run_id=selected_run.tool_run_id,
                selected_result_id=result.result_id,
                started_at=created_at,
                updated_at=created_at + timedelta(minutes=3),
                completed_at=created_at + timedelta(minutes=3),
            )
        )
    return {
        "actor_id": actor_id,
        "conversation_id": conversation_id,
        "task_id": task_id,
        "revision_id": revision_id,
        "selected_run_id": selected_run.tool_run_id,
        "selected_result_id": result.result_id,
    }


def _append_tool_retry(engine, seeded: dict[str, str]) -> dict[str, str]:
    task_id = seeded["task_id"]
    created_at = BASE + timedelta(hours=2)
    run = _terminal_run(
        task_id=task_id,
        revision_id=seeded["revision_id"],
        attempt_no=2,
        status="SUCCEEDED",
        created_at=created_at,
    )
    result = _result_for_run(run, result_id=f"{task_id}_retry_result")
    asset = _available_asset(
        task_id=task_id,
        run_id=run.tool_run_id,
        asset_id=f"{task_id}_retry_asset",
        created_at=created_at,
    )
    with SQLAlchemyUnitOfWork(
        create_session_factory(engine)
    ) as unit_of_work:
        unit_of_work.tool_runs.add(run)
        unit_of_work.tool_results.add(result)
        unit_of_work.assets.add(asset)
        unit_of_work.result_asset_links.add(
            ResultAssetLink(
                result_id=result.result_id,
                asset_id=asset.asset_id,
                artifact_order=0,
                created_at=created_at,
            )
        )
        assert unit_of_work.session is not None
        unit_of_work.session.execute(
            update(TaskRow)
            .where(TaskRow.task_id == task_id)
            .values(
                current_status="SUCCEEDED",
                selected_tool_run_id=run.tool_run_id,
                selected_result_id=result.result_id,
                updated_at=created_at + timedelta(seconds=1),
                completed_at=created_at + timedelta(seconds=1),
            )
        )
        unit_of_work.commit()
    return {
        "run_id": run.tool_run_id,
        "result_id": result.result_id,
        "asset_id": asset.asset_id,
    }


def _append_explanation_retry(
    engine,
    seeded: dict[str, str],
) -> str:
    created_at = BASE + timedelta(hours=2)
    explanation, call = _explanation_attempt(
        task_id=seeded["task_id"],
        result_id=seeded["selected_result_id"],
        attempt_no=2,
        status="SUCCEEDED",
        created_at=created_at,
    )
    with SQLAlchemyUnitOfWork(
        create_session_factory(engine)
    ) as unit_of_work:
        unit_of_work.llm_calls.add(call)
        unit_of_work.explanations.add(explanation)
        assert unit_of_work.session is not None
        unit_of_work.session.execute(
            update(TaskRow)
            .where(TaskRow.task_id == seeded["task_id"])
            .values(
                current_status="SUCCEEDED",
                updated_at=created_at + timedelta(seconds=1),
                completed_at=created_at + timedelta(seconds=1),
            )
        )
        unit_of_work.commit()
    return explanation.explanation_id


def test_query_uses_database_keyset_and_suppresses_tool_messages(
    migrated_database_engine,
) -> None:
    _seed_mixed_timeline(migrated_database_engine)
    repository = SQLAlchemyTimelineQueryRepository(
        migrated_database_engine
    )

    first = repository.fetch_owned_page(
        actor_id="actor_timeline",
        conversation_id="conversation_timeline",
        after=None,
        limit=2,
    )
    assert first is not None
    assert [
        (item.item_type, item.item_id) for item in first.keys
    ] == [
        ("USER_MESSAGE", "message_knowledge_user"),
        ("ASSISTANT_MESSAGE", "message_knowledge_assistant"),
    ]
    assert first.has_more is True

    last = first.keys[-1]
    second = repository.fetch_owned_page(
        actor_id="actor_timeline",
        conversation_id="conversation_timeline",
        after=last,
        limit=20,
    )
    assert second is not None
    assert [
        (item.item_type, item.item_id) for item in second.keys
    ] == [
        ("TOOL_TASK", "task_tool"),
        ("USER_MESSAGE", "message_undecided"),
    ]
    assert second.has_more is False
    assert {
        message.message_id for message in second.messages
    } == {
        "message_tool_initial",
        "message_tool_follow_up",
        "message_undecided",
    }
    assert {
        task.task_id for task in second.tasks
    } == {"task_tool", "task_undecided"}


def test_query_is_repeatable_read_read_only_and_has_fixed_select_count(
    migrated_database_engine,
) -> None:
    _seed_mixed_timeline(migrated_database_engine)
    with SQLAlchemyUnitOfWork(
        create_session_factory(migrated_database_engine)
    ) as unit_of_work:
        for index in range(20):
            task = Task.pending(
                task_id=f"task_bulk_{index:02}",
                conversation_id="conversation_timeline",
                actor_id="actor_timeline",
                created_at=BASE + timedelta(minutes=2 + index),
            )
            task.task_type = "TOOL_EXECUTION"
            unit_of_work.tasks.add(task)
            unit_of_work.messages.add(
                Message.user(
                    message_id=f"message_bulk_{index:02}",
                    conversation_id="conversation_timeline",
                    task_id=task.task_id,
                    actor_id="actor_timeline",
                    request_id=f"request_bulk_{index:02}",
                    content_text=f"bulk {index}",
                    created_at=task.created_at,
                )
            )
        unit_of_work.commit()
    statements: list[str] = []

    def record_statement(
        _connection,
        _cursor,
        statement,
        _parameters,
        _context,
        _executemany,
    ) -> None:
        statements.append(statement.strip())

    event.listen(
        migrated_database_engine,
        "before_cursor_execute",
        record_statement,
    )
    try:
        repository = SQLAlchemyTimelineQueryRepository(
            migrated_database_engine
        )
        for limit in (1, 20):
            statements.clear()
            page = repository.fetch_owned_page(
                actor_id="actor_timeline",
                conversation_id="conversation_timeline",
                after=None,
                limit=limit,
            )
            assert page is not None
            selects = [
                statement
                for statement in statements
                if statement.upper().startswith("SELECT")
            ]
            assert len(selects) <= 10
            assert len(selects) == 9
            assert any(
                statement.upper() == "SET TRANSACTION READ ONLY"
                for statement in statements
            )
            assert any(
                statement.upper() == "SHOW TRANSACTION_ISOLATION"
                for statement in statements
            )
            assert any(
                statement.upper() == "SHOW TRANSACTION_READ_ONLY"
                for statement in statements
            )
    finally:
        event.remove(
            migrated_database_engine,
            "before_cursor_execute",
            record_statement,
        )

    with migrated_database_engine.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(MessageRow)) == 25
        assert connection.scalar(select(func.count()).select_from(TaskRow)) == 23


def test_foreign_and_missing_conversation_both_return_no_page(
    migrated_database_engine,
) -> None:
    _seed_mixed_timeline(migrated_database_engine)
    repository = SQLAlchemyTimelineQueryRepository(
        migrated_database_engine
    )

    foreign = repository.fetch_owned_page(
        actor_id="actor_other",
        conversation_id="conversation_timeline",
        after=None,
        limit=20,
    )
    missing = repository.fetch_owned_page(
        actor_id="actor_timeline",
        conversation_id="conversation_missing",
        after=None,
        limit=20,
    )

    assert foreign is None
    assert missing is None


def test_query_keeps_one_repeatable_read_snapshot_across_batched_phases(
    migrated_database_engine,
) -> None:
    _seed_mixed_timeline(migrated_database_engine)
    state = {"selects": 0, "inserted": False}

    def insert_after_snapshot(
        _connection,
        _cursor,
        statement,
        _parameters,
        _context,
        _executemany,
    ) -> None:
        if not statement.lstrip().upper().startswith("SELECT"):
            return
        state["selects"] += 1
        if state["selects"] != 2 or state["inserted"]:
            return
        state["inserted"] = True
        created_at = BASE + timedelta(hours=1)
        task = Task.pending(
            task_id="task_after_snapshot",
            conversation_id="conversation_timeline",
            actor_id="actor_timeline",
            created_at=created_at,
        )
        task.task_type = "TOOL_EXECUTION"
        with SQLAlchemyUnitOfWork(
            create_session_factory(migrated_database_engine)
        ) as unit_of_work:
            unit_of_work.tasks.add(task)
            unit_of_work.messages.add(
                Message.user(
                    message_id="message_after_snapshot",
                    conversation_id="conversation_timeline",
                    task_id=task.task_id,
                    actor_id="actor_timeline",
                    request_id="request_after_snapshot",
                    content_text="after snapshot",
                    created_at=created_at,
                )
            )
            unit_of_work.commit()

    event.listen(
        migrated_database_engine,
        "before_cursor_execute",
        insert_after_snapshot,
    )
    try:
        repository = SQLAlchemyTimelineQueryRepository(
            migrated_database_engine
        )
        first_snapshot = repository.fetch_owned_page(
            actor_id="actor_timeline",
            conversation_id="conversation_timeline",
            after=None,
            limit=50,
        )
    finally:
        event.remove(
            migrated_database_engine,
            "before_cursor_execute",
            insert_after_snapshot,
        )

    assert first_snapshot is not None
    assert state["inserted"] is True
    assert "task_after_snapshot" not in {
        item.task_id for item in first_snapshot.keys
    }

    next_snapshot = repository.fetch_owned_page(
        actor_id="actor_timeline",
        conversation_id="conversation_timeline",
        after=None,
        limit=50,
    )
    assert next_snapshot is not None
    assert "task_after_snapshot" in {
        item.task_id for item in next_snapshot.keys
    }


def test_task_detail_query_count_is_fixed_for_one_and_twenty_attempts(
    migrated_database_engine,
) -> None:
    small = _seed_task_detail(
        migrated_database_engine,
        task_id="task_detail_small",
        attempt_count=1,
        asset_count=1,
        explanation_statuses=("SUCCEEDED",),
    )
    large = _seed_task_detail(
        migrated_database_engine,
        task_id="task_detail_large",
        attempt_count=20,
        asset_count=8,
        explanation_statuses=("SUCCEEDED",) * 20,
    )
    statements: list[str] = []

    def record_statement(
        _connection,
        _cursor,
        statement,
        _parameters,
        _context,
        _executemany,
    ) -> None:
        statements.append(statement.strip())

    event.listen(
        migrated_database_engine,
        "before_cursor_execute",
        record_statement,
    )
    try:
        repository = SQLAlchemyTimelineQueryRepository(
            migrated_database_engine
        )
        counts: list[tuple[int, int]] = []
        for seeded in (small, large):
            statements.clear()
            snapshot = repository.fetch_owned_task_detail(
                actor_id=seeded["actor_id"],
                task_id=seeded["task_id"],
            )
            assert snapshot is not None
            business_selects = [
                statement
                for statement in statements
                if statement.lstrip().upper().startswith("SELECT")
            ]
            counts.append((len(business_selects), len(statements)))
            assert len(business_selects) <= 9
            assert any(
                statement.upper() == "SET TRANSACTION READ ONLY"
                for statement in statements
            )
            assert any(
                statement.upper() == "SHOW TRANSACTION_ISOLATION"
                for statement in statements
            )
            assert any(
                statement.upper() == "SHOW TRANSACTION_READ_ONLY"
                for statement in statements
            )
        assert counts == [(7, 10), (7, 10)]
        assert len(
            repository.fetch_owned_task_detail(
                actor_id=large["actor_id"],
                task_id=large["task_id"],
            ).tool_runs
        ) == 20
    finally:
        event.remove(
            migrated_database_engine,
            "before_cursor_execute",
            record_statement,
        )


def test_task_detail_tool_retry_keeps_one_selected_chain_snapshot(
    migrated_database_engine,
) -> None:
    seeded = _seed_task_detail(
        migrated_database_engine,
        task_id="task_tool_retry_snapshot",
        attempt_count=1,
        asset_count=1,
        explanation_statuses=("FAILED",),
        selected_status="PARTIALLY_SUCCEEDED",
        task_status="PARTIALLY_SUCCEEDED",
    )
    state: dict[str, object] = {
        "business_selects": 0,
        "inserted": False,
        "retry": None,
    }

    def retry_after_snapshot(
        _connection,
        _cursor,
        statement,
        _parameters,
        _context,
        _executemany,
    ) -> None:
        if (
            state["inserted"]
            or not statement.lstrip().upper().startswith("SELECT")
        ):
            return
        state["business_selects"] = int(state["business_selects"]) + 1
        if state["business_selects"] != 2:
            return
        state["inserted"] = True
        state["retry"] = _append_tool_retry(
            migrated_database_engine,
            seeded,
        )

    event.listen(
        migrated_database_engine,
        "before_cursor_execute",
        retry_after_snapshot,
    )
    try:
        service = TaskQueryService(
            SQLAlchemyTimelineQueryRepository(migrated_database_engine)
        )
        first = service.get_projection(
            ActorContext(actor_id=seeded["actor_id"], user_id=None),
            seeded["task_id"],
        )
    finally:
        event.remove(
            migrated_database_engine,
            "before_cursor_execute",
            retry_after_snapshot,
        )

    assert state["inserted"] is True
    assert first.task.current_status == "PARTIALLY_SUCCEEDED"
    assert first.task.selected_tool_run_id == seeded["selected_run_id"]
    assert first.selected_result is not None
    assert first.selected_result.result_id == seeded["selected_result_id"]
    assert [run.tool_run_id for run in first.tool_runs] == [
        seeded["selected_run_id"]
    ]
    assert [asset.asset_id for asset in first.assets] == [
        f"{seeded['task_id']}_asset_00"
    ]

    second = service.get_projection(
        ActorContext(actor_id=seeded["actor_id"], user_id=None),
        seeded["task_id"],
    )
    retry = state["retry"]
    assert isinstance(retry, dict)
    assert second.task.current_status == "SUCCEEDED"
    assert second.task.selected_tool_run_id == retry["run_id"]
    assert second.selected_result is not None
    assert second.selected_result.result_id == retry["result_id"]
    assert [run.tool_run_id for run in second.tool_runs] == [
        seeded["selected_run_id"],
        retry["run_id"],
    ]
    assert [asset.asset_id for asset in second.assets] == [
        retry["asset_id"]
    ]


def test_task_detail_explanation_retry_keeps_task_and_attempts_in_one_snapshot(
    migrated_database_engine,
) -> None:
    seeded = _seed_task_detail(
        migrated_database_engine,
        task_id="task_explanation_retry_snapshot",
        attempt_count=1,
        asset_count=1,
        explanation_statuses=("FAILED",),
        task_status="PARTIALLY_SUCCEEDED",
    )
    state: dict[str, object] = {
        "business_selects": 0,
        "inserted": False,
        "explanation_id": None,
    }

    def retry_after_snapshot(
        _connection,
        _cursor,
        statement,
        _parameters,
        _context,
        _executemany,
    ) -> None:
        if (
            state["inserted"]
            or not statement.lstrip().upper().startswith("SELECT")
        ):
            return
        state["business_selects"] = int(state["business_selects"]) + 1
        if state["business_selects"] != 2:
            return
        state["inserted"] = True
        state["explanation_id"] = _append_explanation_retry(
            migrated_database_engine,
            seeded,
        )

    event.listen(
        migrated_database_engine,
        "before_cursor_execute",
        retry_after_snapshot,
    )
    try:
        service = TaskQueryService(
            SQLAlchemyTimelineQueryRepository(migrated_database_engine)
        )
        first = service.get_projection(
            ActorContext(actor_id=seeded["actor_id"], user_id=None),
            seeded["task_id"],
        )
    finally:
        event.remove(
            migrated_database_engine,
            "before_cursor_execute",
            retry_after_snapshot,
        )

    assert state["inserted"] is True
    assert first.task.current_status == "PARTIALLY_SUCCEEDED"
    assert first.explanation_summary is not None
    assert first.explanation_summary.status == "FAILED"
    assert first.explanation_summary.attempt_no == 1

    second = service.get_projection(
        ActorContext(actor_id=seeded["actor_id"], user_id=None),
        seeded["task_id"],
    )
    assert second.task.current_status == "SUCCEEDED"
    assert second.explanation_summary is not None
    assert second.explanation_summary.status == "SUCCEEDED"
    assert second.explanation_summary.explanation_id == (
        state["explanation_id"]
    )
    assert second.latest_explanation_failure is None


def test_read_query_closes_session_on_success_not_found_and_error(
    migrated_database_engine,
    monkeypatch,
) -> None:
    _seed_mixed_timeline(migrated_database_engine)
    close_calls: list[int] = []

    class _TrackedSession(Session):
        def close(self) -> None:
            close_calls.append(1)
            super().close()

    monkeypatch.setattr(timeline_query_db, "Session", _TrackedSession)
    repository = SQLAlchemyTimelineQueryRepository(
        migrated_database_engine
    )

    assert repository.fetch_owned_page(
        actor_id="actor_timeline",
        conversation_id="conversation_timeline",
        after=None,
        limit=20,
    ) is not None
    assert repository.fetch_owned_task_detail(
        actor_id="actor_timeline",
        task_id="task_missing",
    ) is None

    def fail_query(*_args, **_kwargs):
        raise SQLAlchemyError("private query detail")

    monkeypatch.setattr(
        SQLAlchemyTimelineQueryRepository,
        "_fetch_owned_task_detail",
        staticmethod(fail_query),
    )
    with pytest.raises(PersistenceError):
        repository.fetch_owned_task_detail(
            actor_id="actor_timeline",
            task_id="task_missing",
        )

    assert len(close_calls) == 3
