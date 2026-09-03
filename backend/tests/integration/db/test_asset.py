from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select
from sqlalchemy.engine import Engine

from materialsagent.domain.models.actor import Actor
from materialsagent.domain.models.asset import Asset
from materialsagent.domain.models.conversation import Conversation
from materialsagent.domain.models.llm_call import LLMCall
from materialsagent.domain.models.task import Task
from materialsagent.domain.models.task_input_revision import TaskInputRevision
from materialsagent.domain.models.tool_run import ToolRun
from materialsagent.domain.ports.tool_registry import ExecutionPolicy
from materialsagent.domain.ports.unit_of_work import PersistenceConflictError
from materialsagent.infrastructure.db.asset import AssetRow
from materialsagent.infrastructure.db.session import create_session_factory
from materialsagent.infrastructure.db.unit_of_work import SQLAlchemyUnitOfWork


BASE = datetime(2026, 7, 22, 3, 0, tzinfo=timezone.utc)


def _factory(engine: Engine):
    return lambda: SQLAlchemyUnitOfWork(create_session_factory(engine))


def _seed_running_tool_run(engine: Engine) -> ToolRun:
    factory = _factory(engine)
    with factory() as unit_of_work:
        unit_of_work.actors.add(Actor.local_anonymous("actor_1", created_at=BASE))
        unit_of_work.actors.add(Actor.local_anonymous("actor_2", created_at=BASE))
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
                generation_parameters={"temperature": 0, "max_tokens": 512},
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
        pending_run = ToolRun.pending(
            tool_run_id="tool_run_1",
            task_id="task_1",
            request_id="execute_request_1",
            task_input_revision_id="revision_1",
            attempt_no=1,
            tool_id="zta35g_sem_virtual_lab",
            tool_version="0.1.0",
            schema_hash="f821240f782ce788bc723fd1acd02a2e58cedbf68b70b1414e2accd16d989d07",
            normalized_input_snapshot={},
            execution_policy_snapshot=ExecutionPolicy.ANY_TASK,
            input_revision_no=1,
            requested_outputs=["sem_image"],
            execution_input={
                "process_parameters": {},
                "requested_outputs": ["sem_image"],
                "runtime_parameters": {"seed": 101},
            },
            created_at=BASE + timedelta(seconds=2),
        )
        run = pending_run.start(
            started_at=BASE + timedelta(seconds=3)
        ).record_runtime_output(
            actual_runtime_parameters={
                "seed": 101,
                "num_samples": 1,
                "guide_scale": 2.0,
                "timesteps": 1000,
            },
            diagnostics=[],
            output_summary={
                "runtime_status": "SUCCEEDED",
                "image_count": 1,
                "image_roles": ["generated_sem"],
            },
            model_bundle_id="mock-bundle",
        )
        unit_of_work.tool_runs.add(pending_run)
        assert unit_of_work.tool_runs.update(
            run,
            expected_status="PENDING",
        ) == run
        unit_of_work.commit()
    return run


def _pending(asset_id: str = "asset_1", object_key: str = "assets/test/asset_1.png") -> Asset:
    return Asset.pending(
        asset_id=asset_id,
        task_id="task_1",
        producer_tool_run_id="tool_run_1",
        actor_id="actor_1",
        operation_id=f"operation_{asset_id}",
        role="requested_output",
        object_key=object_key,
        pending_since=BASE + timedelta(seconds=4),
        created_at=BASE + timedelta(seconds=4),
    )


def test_asset_round_trip_owned_query_and_conditional_finalize(
    migrated_database_engine: Engine,
) -> None:
    original_run = _seed_running_tool_run(migrated_database_engine)
    factory = _factory(migrated_database_engine)
    pending = _pending()
    with factory() as unit_of_work:
        unit_of_work.assets.add(pending)
        unit_of_work.commit()

    with factory() as unit_of_work:
        assert unit_of_work.assets.get("asset_1") == pending
        assert unit_of_work.assets.get_owned("asset_1", "actor_1") == pending
        assert unit_of_work.assets.get_owned("asset_1", "actor_2") is None
        assert unit_of_work.assets.list_for_tool_run("tool_run_1") == [pending]
        available = pending.mark_available(
            sha256="a" * 64,
            size_bytes=1234,
            width=512,
            height=512,
            bit_depth=8,
            media_type="image/png",
            encoding_rule="linear[-1,1]-half-up-uint8-png-l",
            available_at=BASE + timedelta(seconds=5),
        )
        assert unit_of_work.assets.update(
            available,
            expected_status="PENDING",
        ) == available
        assert unit_of_work.assets.update(
            available,
            expected_status="PENDING",
        ) is None
        unit_of_work.commit()

    with factory() as unit_of_work:
        assert unit_of_work.tool_runs.get("tool_run_1") == original_run
        task = unit_of_work.tasks.get("task_1")
        assert task.current_status == "FAILED"
        assert task.selected_tool_run_id is None
        assert task.selected_result_id is None


def test_asset_object_key_is_unique_and_source_fields_cannot_be_rebound(
    migrated_database_engine: Engine,
) -> None:
    _seed_running_tool_run(migrated_database_engine)
    factory = _factory(migrated_database_engine)
    pending = _pending()
    with factory() as unit_of_work:
        unit_of_work.assets.add(pending)
        unit_of_work.commit()

    with pytest.raises(PersistenceConflictError):
        with factory() as unit_of_work:
            unit_of_work.assets.add(
                _pending(asset_id="asset_2", object_key=pending.object_key)
            )
            unit_of_work.commit()

    with factory() as unit_of_work:
        rebound = replace(pending, actor_id="actor_2")
        assert unit_of_work.assets.update(
            rebound,
            expected_status="PENDING",
        ) is None
        assert unit_of_work.assets.get("asset_1") == pending
    with migrated_database_engine.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(AssetRow)) == 1
