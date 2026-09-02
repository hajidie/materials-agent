from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone

from pydantic import SecretStr
import pytest

from materialsagent.application.context import ActorContext
from materialsagent.application.timeline import (
    MessageTimelineItem,
    TimelineQueryService,
    ToolTaskTimelineItem,
)
from materialsagent.application.timeline_cursor import TimelineCursorCodec
from materialsagent.domain.models.conversation import Conversation
from materialsagent.domain.models.asset import Asset
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
from materialsagent.domain.ports.tool_registry import ExecutionPolicy
from materialsagent.domain.ports.timeline_query import (
    TimelineKey,
    TimelineQueryPage,
)


BASE = datetime(2026, 7, 24, 3, 0, tzinfo=timezone.utc)
SCHEMA_HASH = "f821240f782ce788bc723fd1acd02a2e58cedbf68b70b1414e2accd16d989d07"


def _conversation() -> Conversation:
    return Conversation(
        conversation_id="conversation_1",
        actor_id="actor_1",
        title="Timeline",
        created_at=BASE,
        updated_at=BASE,
    )


def _task(
    task_id: str,
    *,
    task_type: str | None,
    created_at: datetime = BASE,
) -> Task:
    task = Task.pending(
        task_id=task_id,
        conversation_id="conversation_1",
        actor_id="actor_1",
        created_at=created_at,
    )
    task.task_type = task_type
    return task


def _message(
    message_id: str,
    task_id: str,
    *,
    role: str = "USER",
    created_at: datetime = BASE,
) -> Message:
    if role == "USER":
        return Message.user(
            message_id=message_id,
            conversation_id="conversation_1",
            task_id=task_id,
            actor_id="actor_1",
            request_id=f"request_{message_id}",
            content_text=message_id,
            created_at=created_at,
        )
    return Message(
        message_id=message_id,
        conversation_id="conversation_1",
        task_id=task_id,
        actor_id="actor_1",
        request_id=f"request_{message_id}",
        role="ASSISTANT",
        generation_source="TEMPLATE",
        content_text=message_id,
        structured_content=None,
        llm_call_id=None,
        created_at=created_at,
    )


class _FakeTimelineQuery:
    def __init__(self, page: TimelineQueryPage | None) -> None:
        self.page = page
        self.calls: list[tuple[str, str, object, int]] = []

    def fetch_owned_page(
        self,
        *,
        actor_id: str,
        conversation_id: str,
        after,
        limit: int,
    ) -> TimelineQueryPage | None:
        self.calls.append((actor_id, conversation_id, after, limit))
        return self.page


def _service(page: TimelineQueryPage) -> TimelineQueryService:
    return TimelineQueryService(
        _FakeTimelineQuery(page),
        TimelineCursorCodec(
            SecretStr("timeline-signing-key-with-at-least-32-bytes")
        ),
    )


def test_same_timestamp_items_use_fixed_rank_then_item_id() -> None:
    knowledge = _task("task_knowledge", task_type="KNOWLEDGE_QA")
    tool = _task("task_tool", task_type="TOOL_EXECUTION")
    user_a = _message("message_a", knowledge.task_id)
    user_z = _message("message_z", knowledge.task_id)
    assistant = _message(
        "message_b",
        knowledge.task_id,
        role="ASSISTANT",
    )
    tool_initial = _message("message_tool", tool.task_id)
    keys = (
        TimelineKey(
            item_type="USER_MESSAGE",
            item_id=user_a.message_id,
            task_id=knowledge.task_id,
            anchor_at=BASE,
            item_type_rank=10,
        ),
        TimelineKey(
            item_type="USER_MESSAGE",
            item_id=user_z.message_id,
            task_id=knowledge.task_id,
            anchor_at=BASE,
            item_type_rank=10,
        ),
        TimelineKey(
            item_type="ASSISTANT_MESSAGE",
            item_id=assistant.message_id,
            task_id=knowledge.task_id,
            anchor_at=BASE,
            item_type_rank=20,
        ),
        TimelineKey(
            item_type="TOOL_TASK",
            item_id=tool.task_id,
            task_id=tool.task_id,
            anchor_at=BASE,
            item_type_rank=30,
        ),
    )
    page = TimelineQueryPage(
        conversation=_conversation(),
        keys=keys,
        has_more=False,
        messages=(user_a, user_z, assistant, tool_initial),
        tasks=(knowledge, tool),
    )

    result = _service(page).get_page(
        ActorContext(actor_id="actor_1", user_id=None),
        conversation_id="conversation_1",
        limit=20,
        cursor=None,
    )

    assert [
        (item.item_type, item.item_id) for item in result.items
    ] == [
        ("USER_MESSAGE", "message_a"),
        ("USER_MESSAGE", "message_z"),
        ("ASSISTANT_MESSAGE", "message_b"),
        ("TOOL_TASK", "task_tool"),
    ]
    assert isinstance(result.items[0], MessageTimelineItem)
    assert isinstance(result.items[-1], ToolTaskTimelineItem)
    assert result.items[-1].initial_user_message.message_id == "message_tool"
    assert all(item.item_id != "message_tool" for item in result.items)


def test_tool_input_thread_keeps_initial_and_latest_50_in_ascending_order(
) -> None:
    tool = _task("task_tool", task_type="TOOL_EXECUTION")
    messages = tuple(
        _message(
            f"message_{index:03}",
            tool.task_id,
            role=("USER" if index % 2 == 0 else "ASSISTANT"),
            created_at=BASE + timedelta(seconds=index),
        )
        for index in range(56)
    )
    page = TimelineQueryPage(
        conversation=_conversation(),
        keys=(
            TimelineKey(
                item_type="TOOL_TASK",
                item_id=tool.task_id,
                task_id=tool.task_id,
                anchor_at=BASE,
                item_type_rank=30,
            ),
        ),
        has_more=False,
        messages=tuple(reversed(messages)),
        tasks=(tool,),
    )

    result = _service(page).get_page(
        ActorContext(actor_id="actor_1", user_id=None),
        conversation_id="conversation_1",
        limit=20,
        cursor=None,
    )

    item = result.items[0]
    assert isinstance(item, ToolTaskTimelineItem)
    assert item.initial_user_message.message_id == "message_000"
    assert item.input_thread_count == 55
    assert item.input_thread_truncated is True
    assert [message.message_id for message in item.input_thread] == [
        f"message_{index:03}" for index in range(6, 56)
    ]


def test_tool_anchor_falls_back_to_task_created_at_with_safe_error(
) -> None:
    tool = _task(
        "task_without_user",
        task_type="TOOL_EXECUTION",
        created_at=BASE + timedelta(minutes=1),
    )
    page = TimelineQueryPage(
        conversation=_conversation(),
        keys=(
            TimelineKey(
                item_type="TOOL_TASK",
                item_id=tool.task_id,
                task_id=tool.task_id,
                anchor_at=tool.created_at,
                item_type_rank=30,
            ),
        ),
        has_more=False,
        messages=(),
        tasks=(tool,),
    )

    item = _service(page).get_page(
        ActorContext(actor_id="actor_1", user_id=None),
        conversation_id="conversation_1",
        limit=20,
        cursor=None,
    ).items[0]

    assert isinstance(item, ToolTaskTimelineItem)
    assert item.anchor_at == tool.created_at
    assert item.initial_user_message is None
    assert item.errors == (
        replace(
            item.errors[0],
            code="CONTEXT_UNAVAILABLE",
            message="任务输入上下文不可用。",
        ),
    )


def test_next_cursor_uses_last_returned_item_and_replays_as_keyset_boundary(
) -> None:
    task = _task("task_knowledge", task_type=None)
    message = _message("message_1", task.task_id)
    page = TimelineQueryPage(
        conversation=_conversation(),
        keys=(
            TimelineKey(
                item_type="USER_MESSAGE",
                item_id=message.message_id,
                task_id=task.task_id,
                anchor_at=BASE,
                item_type_rank=10,
            ),
        ),
        has_more=True,
        messages=(message,),
        tasks=(task,),
    )
    fake = _FakeTimelineQuery(page)
    codec = TimelineCursorCodec(
        SecretStr("timeline-signing-key-with-at-least-32-bytes")
    )
    service = TimelineQueryService(fake, codec)

    first = service.get_page(
        ActorContext(actor_id="actor_1", user_id=None),
        conversation_id="conversation_1",
        limit=1,
        cursor=None,
    )
    service.get_page(
        ActorContext(actor_id="actor_1", user_id=None),
        conversation_id="conversation_1",
        limit=1,
        cursor=first.next_cursor,
    )

    assert first.next_cursor is not None
    assert fake.calls[1][2] == codec.decode(
        first.next_cursor,
        expected_conversation_id="conversation_1",
    )


def _terminal_run(
    run_id: str,
    attempt_no: int,
    *,
    created_at: datetime,
) -> ToolRun:
    pending = ToolRun.pending(
        tool_run_id=run_id,
        task_id="task_tool",
        request_id=f"request_{run_id}",
        task_input_revision_id="revision_1",
        attempt_no=attempt_no,
        tool_id="zta35g_sem_virtual_lab",
        tool_version="0.1.0",
        schema_hash=SCHEMA_HASH,
        normalized_input_snapshot={},
        execution_policy_snapshot=ExecutionPolicy.ANY_TASK,
        input_revision_no=1,
        execution_input={"private": "must-not-be-projected"},
        requested_outputs=["sem_image"],
        created_at=created_at,
    )
    running = pending.start(started_at=created_at)
    recorded = running.record_runtime_output(
        actual_runtime_parameters={"private": "must-not-be-projected"},
        diagnostics=[
            {
                "step": "inference",
                "status": "ok",
                "duration_ms": 12,
                "error_code": None,
                "safe_error_message": None,
                "private_path": "C:/private/model",
            }
        ],
        output_summary={"private": "must-not-be-projected"},
        model_bundle_id="private-model-bundle",
    )
    return recorded.complete_from_result(
        completed_outputs=["sem_image"],
        failed_outputs=[],
        completed_at=created_at + timedelta(seconds=1),
        error_code=None,
        safe_error_message=None,
    )


def _selected_result(**overrides: object) -> ToolResult:
    values: dict[str, object] = {
        "result_id": "result_selected",
        "task_id": "task_tool",
        "tool_run_id": "run_2",
        "actor_id": "actor_1",
        "status": "SUCCEEDED",
        "requested_outputs": ["sem_image"],
        "completed_outputs": ["sem_image"],
        "failed_outputs": [],
        "data": {},
        "warnings": [],
        "provenance": {
            "input_revision": 1,
            "normalized_process_parameters": {},
            "actual_runtime_parameters": {},
        },
        "error": None,
        "tool_id": "zta35g_sem_virtual_lab",
        "tool_version": "0.1.0",
        "schema_hash": SCHEMA_HASH,
        "created_at": BASE + timedelta(seconds=5),
    }
    values.update(overrides)
    return ToolResult(**values)  # type: ignore[arg-type]


def _available_asset() -> Asset:
    return Asset.pending(
        asset_id="asset_selected",
        task_id="task_tool",
        producer_tool_run_id="run_2",
        actor_id="actor_1",
        operation_id="operation_asset_selected",
        role="requested_output",
        object_key="assets/test/asset_selected.png",
        pending_since=BASE + timedelta(seconds=5),
        created_at=BASE + timedelta(seconds=5),
    ).mark_available(
        sha256="a" * 64,
        size_bytes=123,
        width=512,
        height=512,
        bit_depth=8,
        media_type="image/png",
        encoding_rule="linear[-1,1]-half-up-uint8-png-l",
        available_at=BASE + timedelta(seconds=6),
    )


def _explanation_attempt(
    attempt_no: int,
    *,
    status: str,
) -> tuple[NaturalLanguageExplanation, LLMCall]:
    created_at = BASE + timedelta(seconds=10 + attempt_no)
    completed_at = created_at + timedelta(seconds=1)
    explanation_id = f"explanation_{attempt_no}"
    llm_call_id = f"llm_call_{attempt_no}"
    succeeded = status == "SUCCEEDED"
    explanation = NaturalLanguageExplanation(
        explanation_id=explanation_id,
        task_id="task_tool",
        result_id="result_selected",
        llm_call_id=llm_call_id,
        attempt_no=attempt_no,
        status=status,
        language="zh-CN",
        text=("安全解释" if succeeded else None),
        created_at=created_at,
        started_at=created_at,
        completed_at=completed_at,
        duration_ms=1000,
        error_code=(None if succeeded else "EXPLANATION_FAILED"),
        safe_error_message=(None if succeeded else "解释生成失败。"),
    )
    call = LLMCall(
        llm_call_id=llm_call_id,
        task_id="task_tool",
        conversation_id="conversation_1",
        request_id=f"request_explanation_{attempt_no}",
        purpose="TOOL_RESULT_EXPLANATION",
        input_result_id="result_selected",
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
        provider_request_id=(
            f"provider-{attempt_no}" if succeeded else None
        ),
        status=status,
        created_at=created_at,
        started_at=created_at,
        completed_at=completed_at,
        duration_ms=1000,
        error_code=(None if succeeded else "EXPLANATION_FAILED"),
        safe_error_message=(None if succeeded else "解释生成失败。"),
    )
    return explanation, call


def _complete_tool_page(**overrides: object) -> TimelineQueryPage:
    task = _task("task_tool", task_type="TOOL_EXECUTION")
    task.current_status = "SUCCEEDED"
    task.selected_tool_run_id = "run_2"
    task.selected_result_id = "result_selected"
    initial = _message("message_initial", task.task_id)
    run_1 = _terminal_run("run_1", 1, created_at=BASE)
    run_2 = _terminal_run(
        "run_2",
        2,
        created_at=BASE + timedelta(seconds=2),
    )
    result = _selected_result()
    asset = _available_asset()
    success, success_call = _explanation_attempt(1, status="SUCCEEDED")
    failure, failure_call = _explanation_attempt(2, status="FAILED")
    values: dict[str, object] = {
        "conversation": _conversation(),
        "keys": (
            TimelineKey(
                item_type="TOOL_TASK",
                item_id=task.task_id,
                task_id=task.task_id,
                anchor_at=BASE,
                item_type_rank=30,
            ),
        ),
        "has_more": False,
        "messages": (initial,),
        "tasks": (task,),
        "tool_runs": (run_2, run_1),
        "results": (result,),
        "result_asset_links": (
            ResultAssetLink(
                result_id=result.result_id,
                asset_id=asset.asset_id,
                artifact_order=0,
                created_at=BASE + timedelta(seconds=6),
            ),
        ),
        "assets": (asset,),
        "explanations": (failure, success),
        "llm_calls": (failure_call, success_call),
    }
    values.update(overrides)
    return TimelineQueryPage(**values)  # type: ignore[arg-type]


def test_tool_card_projects_only_selected_safe_chain_and_preserves_history(
) -> None:
    item = _service(_complete_tool_page()).get_page(
        ActorContext(actor_id="actor_1", user_id=None),
        conversation_id="conversation_1",
        limit=20,
        cursor=None,
    ).items[0]

    assert isinstance(item, ToolTaskTimelineItem)
    assert item.tool_runs.attempt_count == 2
    assert item.tool_runs.has_history is True
    selected = item.tool_runs.selected_tool_run
    assert selected.tool_run_id == "run_2"
    assert selected.is_selected is True
    assert selected.diagnostics_summary == (
        {
            "step": "inference",
            "status": "ok",
            "duration_ms": 12,
            "error_code": None,
            "safe_error_message": None,
        },
    )
    assert "private" not in repr(selected)
    assert "model_bundle" not in repr(selected)
    assert item.result.result_id == "result_selected"
    assert item.assets[0].content_url == (
        "/api/v1/assets/asset_selected/content"
    )
    assert "object_key" not in item.assets[0].__dataclass_fields__
    assert item.explanation.text == "安全解释"
    assert item.latest_explanation_failure.attempt_no == 2
    assert item.latest_explanation_failure.error_code == (
        "EXPLANATION_FAILED"
    )


def test_selected_chain_corruption_is_an_internal_error() -> None:
    from materialsagent.application.errors import ApplicationInternalError

    corrupted = _selected_result(tool_run_id="run_1")

    try:
        _service(
            _complete_tool_page(results=(corrupted,))
        ).get_page(
            ActorContext(actor_id="actor_1", user_id=None),
            conversation_id="conversation_1",
            limit=20,
            cursor=None,
        )
    except ApplicationInternalError as error:
        assert error.status_code == 500
        assert str(error) == "内部处理失败。"
    else:
        raise AssertionError("Corrupt selected chain was accepted.")


def _corrupt_missing_result() -> TimelineQueryPage:
    return _complete_tool_page(results=())


def _corrupt_output_sets() -> TimelineQueryPage:
    result = _selected_result()
    object.__setattr__(result, "completed_outputs", ())
    object.__setattr__(result, "failed_outputs", ("sem_image",))
    return _complete_tool_page(results=(result,))


def _corrupt_asset_task() -> TimelineQueryPage:
    asset = replace(_available_asset(), task_id="task_other")
    return _complete_tool_page(assets=(asset,))


def _corrupt_asset_run() -> TimelineQueryPage:
    asset = replace(
        _available_asset(),
        producer_tool_run_id="run_1",
    )
    return _complete_tool_page(assets=(asset,))


def _corrupt_asset_unavailable() -> TimelineQueryPage:
    asset = Asset.pending(
        asset_id="asset_selected",
        task_id="task_tool",
        producer_tool_run_id="run_2",
        actor_id="actor_1",
        operation_id="operation_asset_selected_pending",
        role="requested_output",
        object_key="assets/test/asset_selected_pending.png",
        pending_since=BASE + timedelta(seconds=5),
        created_at=BASE + timedelta(seconds=5),
    )
    return _complete_tool_page(assets=(asset,))


def _corrupt_explanation_source() -> TimelineQueryPage:
    success, success_call = _explanation_attempt(1, status="SUCCEEDED")
    bad_call = replace(success_call, input_result_id="result_other")
    return _complete_tool_page(
        explanations=(success,),
        llm_calls=(bad_call,),
    )


def _corrupt_explanation_result() -> TimelineQueryPage:
    success, success_call = _explanation_attempt(1, status="SUCCEEDED")
    object.__setattr__(success, "result_id", "result_other")
    return _complete_tool_page(
        explanations=(success,),
        llm_calls=(success_call,),
    )


def _corrupt_selected_run_task() -> TimelineQueryPage:
    run_1 = _terminal_run("run_1", 1, created_at=BASE)
    run_2 = _terminal_run(
        "run_2",
        2,
        created_at=BASE + timedelta(seconds=2),
    )
    run_2.task_id = "task_other"
    return _complete_tool_page(tool_runs=(run_1, run_2))


def _corrupt_message_actor() -> TimelineQueryPage:
    message = replace(
        _message("message_initial", "task_tool"),
        actor_id="actor_other",
    )
    return _complete_tool_page(messages=(message,))


@pytest.mark.parametrize(
    "page_factory",
    [
        _corrupt_missing_result,
        _corrupt_output_sets,
        _corrupt_asset_task,
        _corrupt_asset_run,
        _corrupt_asset_unavailable,
        _corrupt_explanation_source,
        _corrupt_explanation_result,
        _corrupt_selected_run_task,
        _corrupt_message_actor,
    ],
)
def test_timeline_source_integrity_matrix_maps_corruption_to_internal_error(
    page_factory,
) -> None:
    from materialsagent.application.errors import ApplicationInternalError

    with pytest.raises(ApplicationInternalError) as exc_info:
        _service(page_factory()).get_page(
            ActorContext(actor_id="actor_1", user_id=None),
            conversation_id="conversation_1",
            limit=20,
            cursor=None,
        )

    assert exc_info.value.status_code == 500
    assert str(exc_info.value) == "内部处理失败。"


def test_needs_input_uses_latest_revision_only() -> None:
    task = _task("task_tool", task_type="TOOL_EXECUTION")
    task.current_status = "NEEDS_INPUT"
    initial = _message("message_initial", task.task_id)
    revisions = tuple(
        TaskInputRevision(
            task_input_revision_id=f"revision_{revision}",
            task_id=task.task_id,
            request_id=f"request_revision_{revision}",
            source_llm_call_id=None,
            source_message_ids=[initial.message_id],
            revision=revision,
            raw_input={},
            normalized_input={"revision": revision},
            missing_fields=[f"field_{revision}"],
            ambiguous_fields=[{"field": f"field_{revision}"}],
            validation_errors=[],
            created_at=BASE + timedelta(seconds=revision),
        )
        for revision in (1, 2)
    )
    page = TimelineQueryPage(
        conversation=_conversation(),
        keys=(
            TimelineKey(
                item_type="TOOL_TASK",
                item_id=task.task_id,
                task_id=task.task_id,
                anchor_at=BASE,
                item_type_rank=30,
            ),
        ),
        has_more=False,
        messages=(initial,),
        tasks=(task,),
        revisions=revisions,
    )

    item = _service(page).get_page(
        ActorContext(actor_id="actor_1", user_id=None),
        conversation_id="conversation_1",
        limit=20,
        cursor=None,
    ).items[0]

    assert isinstance(item, ToolTaskTimelineItem)
    assert item.needs_input.missing_fields == ("field_2",)
    assert item.needs_input.normalized_input == {"revision": 2}
