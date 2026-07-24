from __future__ import annotations

from dataclasses import dataclass, fields, replace
from datetime import datetime, timedelta, timezone

import pytest

from materialsagent.application.context import ActorContext
from materialsagent.application.errors import (
    ApplicationInternalError,
    DependencyUnavailableError,
    ResourceNotFoundError,
)
from materialsagent.application.tasks import TaskQueryService
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
from materialsagent.domain.ports import timeline_query as timeline_query_port
from materialsagent.domain.ports.unit_of_work import (
    DatabaseUnavailableError,
    PersistenceError,
)


BASE = datetime(2026, 7, 24, 6, 0, tzinfo=timezone.utc)


@dataclass(frozen=True, slots=True)
class _Snapshot:
    task: Task
    messages: tuple[Message, ...] = ()
    revisions: tuple[TaskInputRevision, ...] = ()
    tool_runs: tuple[ToolRun, ...] = ()
    selected_result: ToolResult | None = None
    result_asset_links: tuple[ResultAssetLink, ...] = ()
    assets: tuple[Asset, ...] = ()
    explanations: tuple[NaturalLanguageExplanation, ...] = ()
    llm_calls: tuple[LLMCall, ...] = ()


class _FakeTaskDetailQuery:
    def __init__(
        self,
        snapshot: _Snapshot | None = None,
        *,
        error: PersistenceError | None = None,
    ) -> None:
        self.snapshot = snapshot
        self.error = error
        self.calls: list[tuple[str, str]] = []

    def fetch_owned_task_detail(
        self,
        *,
        actor_id: str,
        task_id: str,
    ) -> _Snapshot | None:
        self.calls.append((actor_id, task_id))
        if self.error is not None:
            raise self.error
        return self.snapshot


def _tool_task() -> Task:
    task = Task.pending(
        task_id="task_1",
        conversation_id="conversation_1",
        actor_id="actor_1",
        created_at=BASE,
    )
    task.task_type = "TOOL_EXECUTION"
    return task


def _initial_message() -> Message:
    return Message.user(
        message_id="message_1",
        conversation_id="conversation_1",
        task_id="task_1",
        actor_id="actor_1",
        request_id="request_1",
        content_text="tool request",
        created_at=BASE,
    )


def _terminal_run(
    run_id: str,
    attempt_no: int,
    *,
    created_at: datetime,
) -> ToolRun:
    pending = ToolRun.pending(
        tool_run_id=run_id,
        task_id="task_1",
        request_id=f"request_{run_id}",
        task_input_revision_id="revision_1",
        attempt_no=attempt_no,
        tool_id="zta35g_sem_virtual_lab",
        tool_version="0.1.0",
        schema_version="1.0",
        execution_input={},
        requested_outputs=["sem_image"],
        created_at=created_at,
    )
    running = pending.start(started_at=created_at)
    recorded = running.record_runtime_output(
        actual_runtime_parameters={},
        diagnostics=[],
        output_summary={},
        model_bundle_id="bundle",
    )
    return recorded.complete_from_result(
        completed_outputs=["sem_image"],
        failed_outputs=[],
        completed_at=created_at + timedelta(seconds=1),
        error_code=None,
        safe_error_message=None,
    )


def _selected_result(*, tool_run_id: str = "run_2") -> ToolResult:
    return ToolResult(
        result_id="result_1",
        task_id="task_1",
        tool_run_id=tool_run_id,
        actor_id="actor_1",
        status="SUCCEEDED",
        requested_outputs=["sem_image"],
        completed_outputs=["sem_image"],
        failed_outputs=[],
        data={},
        warnings=[],
        provenance={
            "input_revision": 1,
            "normalized_process_parameters": {},
            "actual_runtime_parameters": {},
        },
        error=None,
        tool_id="zta35g_sem_virtual_lab",
        tool_version="0.1.0",
        schema_version="1.0",
        created_at=BASE + timedelta(seconds=5),
    )


def _available_asset() -> Asset:
    return Asset.pending(
        asset_id="asset_1",
        task_id="task_1",
        producer_tool_run_id="run_2",
        actor_id="actor_1",
        operation_id="operation_1",
        role="requested_output",
        object_key="assets/test/asset_1.png",
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
        task_id="task_1",
        result_id="result_1",
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
        task_id="task_1",
        conversation_id="conversation_1",
        request_id=f"request_explanation_{attempt_no}",
        purpose="TOOL_RESULT_EXPLANATION",
        input_result_id="result_1",
        provider="mock",
        model_name="mock-model",
        prompt_template_id="explanation",
        prompt_template_version="1",
        prompt_digest="a" * 64,
        generation_parameters={"temperature": 0, "max_tokens": 512},
        structured_output_summary=None,
        usage=None,
        provider_request_id=None,
        status=status,
        created_at=created_at,
        started_at=created_at,
        completed_at=completed_at,
        duration_ms=1000,
        error_code=(None if succeeded else "EXPLANATION_FAILED"),
        safe_error_message=(None if succeeded else "解释生成失败。"),
    )
    return explanation, call


def _complete_snapshot(**overrides: object) -> _Snapshot:
    task = _tool_task()
    task.current_status = "SUCCEEDED"
    task.selected_tool_run_id = "run_2"
    task.selected_result_id = "result_1"
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
        "task": task,
        "messages": (_initial_message(),),
        "tool_runs": (run_2, run_1),
        "selected_result": result,
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
    return _Snapshot(**values)  # type: ignore[arg-type]


def test_task_detail_query_snapshot_has_the_fixed_domain_shape() -> None:
    snapshot_type = getattr(
        timeline_query_port,
        "TaskDetailQuerySnapshot",
        None,
    )

    assert snapshot_type is not None
    assert [field.name for field in fields(snapshot_type)] == [
        "task",
        "messages",
        "revisions",
        "tool_runs",
        "selected_result",
        "result_asset_links",
        "assets",
        "explanations",
        "llm_calls",
    ]


def test_task_query_calls_detail_port_once_with_owned_identity_and_projects(
) -> None:
    query = _FakeTaskDetailQuery(_complete_snapshot())
    service = TaskQueryService(query)  # type: ignore[arg-type]

    projection = service.get_projection(
        ActorContext(actor_id="actor_1", user_id=None),
        "task_1",
    )

    assert query.calls == [("actor_1", "task_1")]
    assert projection.anchor_at == BASE
    assert projection.needs_input is None
    assert projection.tool_run_count == 2
    assert [
        (run.tool_run_id, run.attempt_no, run.is_selected)
        for run in projection.tool_runs
    ] == [
        ("run_1", 1, False),
        ("run_2", 2, True),
    ]
    assert projection.selected_result is not None
    assert projection.selected_result.result_id == "result_1"
    assert [asset.asset_id for asset in projection.assets] == ["asset_1"]
    assert projection.explanation_summary is not None
    assert projection.explanation_summary.attempt_no == 1
    assert projection.explanation_summary.text == "安全解释"
    assert projection.latest_explanation_failure is not None
    assert projection.latest_explanation_failure.attempt_no == 2


def test_task_query_selected_reference_corruption_is_internal_not_validation(
) -> None:
    snapshot = _complete_snapshot()
    corrupted = _selected_result(tool_run_id="run_1")
    query = _FakeTaskDetailQuery(
        replace(snapshot, selected_result=corrupted)
    )

    with pytest.raises(ApplicationInternalError) as exc_info:
        TaskQueryService(query).get_projection(  # type: ignore[arg-type]
            ActorContext(actor_id="actor_1", user_id=None),
            "task_1",
        )

    assert exc_info.value.status_code == 500
    assert str(exc_info.value) == "内部处理失败。"
    assert query.calls == [("actor_1", "task_1")]


def test_task_query_missing_owned_snapshot_is_safe_not_found() -> None:
    query = _FakeTaskDetailQuery(None)

    with pytest.raises(ResourceNotFoundError) as exc_info:
        TaskQueryService(query).get_projection(  # type: ignore[arg-type]
            ActorContext(actor_id="actor_1", user_id=None),
            "task_missing",
        )

    assert exc_info.value.status_code == 404
    assert query.calls == [("actor_1", "task_missing")]


@pytest.mark.parametrize(
    ("error", "expected_error", "status_code"),
    [
        (
            DatabaseUnavailableError("private database detail"),
            DependencyUnavailableError,
            503,
        ),
        (
            PersistenceError("private persistence detail"),
            ApplicationInternalError,
            500,
        ),
    ],
)
def test_task_query_maps_query_persistence_failures_safely(
    error: PersistenceError,
    expected_error: type[Exception],
    status_code: int,
) -> None:
    query = _FakeTaskDetailQuery(error=error)

    with pytest.raises(expected_error) as exc_info:
        TaskQueryService(query).get_projection(  # type: ignore[arg-type]
            ActorContext(actor_id="actor_1", user_id=None),
            "task_1",
        )

    assert getattr(exc_info.value, "status_code") == status_code
    assert "private" not in str(exc_info.value)
    assert query.calls == [("actor_1", "task_1")]
