from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from itertools import count

import pytest

from materialsagent.application.context import ActorContext
from materialsagent.application.errors import (
    ApplicationConflictError,
    ApplicationInternalError,
    ResourceNotFoundError,
)
from materialsagent.application.tool_execution import (
    ToolExecutionOutcomeError,
    ToolExecutionService,
)
from materialsagent.application.tools import build_tool_registry
from materialsagent.domain.models.llm_call import LLMCall
from materialsagent.domain.models.task import Task
from materialsagent.domain.models.task_input_revision import TaskInputRevision
from materialsagent.domain.ports.tool_execution import (
    ToolClientProtocolError,
    ToolClientRuntimeError,
    ToolClientTimeoutError,
    ToolClientUnavailableError,
    ToolExecutionOutput,
    ToolImagePayload,
)
from materialsagent.domain.ports.unit_of_work import PersistenceError


UTC = timezone.utc
BASE = datetime(2026, 7, 20, 8, 0, tzinfo=UTC)


def _task() -> Task:
    return Task(
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


def _revision(*, task_id: str = "task_1") -> TaskInputRevision:
    return TaskInputRevision(
        task_input_revision_id="revision_1",
        task_id=task_id,
        request_id="message_request_1",
        source_llm_call_id="llm_1",
        source_message_ids=["message_1"],
        revision=1,
        raw_input={"material": "ZTA35G"},
        normalized_input={
            "material": "ZTA35G",
            "solution_temperature": {"value": 1000, "unit": "°C"},
            "solution_time": {"value": 2.5, "unit": "h"},
            "aging_temperature": {"value": 730, "unit": "°C"},
            "aging_time": {"value": 3.0, "unit": "h"},
            "requested_outputs": ["sem_image", "mechanical_properties"],
        },
        missing_fields=[],
        ambiguous_fields=[],
        validation_errors=[],
        created_at=BASE + timedelta(milliseconds=100),
    )


def _llm_call() -> LLMCall:
    return LLMCall(
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


@dataclass
class _Store:
    tasks: dict[str, Task]
    revisions: dict[str, TaskInputRevision]
    llm_calls: dict[str, LLMCall]
    tool_runs: dict[str, object]
    active_uows: int = 0
    commit_count: int = 0
    fail_commit_at: int | None = None
    fail_failure_update: bool = False


class _Repository:
    def __init__(self, values: dict[str, object]) -> None:
        self.values = values

    def get(self, resource_id: str):
        return self.values.get(resource_id)

    def add(self, value: object) -> None:
        resource_id = next(
            getattr(value, field)
            for field in (
                "tool_run_id",
                "task_input_revision_id",
                "llm_call_id",
                "task_id",
            )
            if hasattr(value, field)
        )
        self.values[resource_id] = value


class _TaskRepository(_Repository):
    def get_owned(self, task_id: str, actor_id: str):
        value = self.get(task_id)
        return value if value is not None and value.actor_id == actor_id else None


class _ToolRunRepository(_Repository):
    def get_owned(self, tool_run_id: str, actor_id: str):
        value = self.get(tool_run_id)
        if value is None:
            return None
        task = self._store.tasks[value.task_id]
        return value if task.actor_id == actor_id else None

    def list_for_task(self, task_id: str):
        return sorted(
            (value for value in self.values.values() if value.task_id == task_id),
            key=lambda item: item.attempt_no,
        )

    def update(self, value: object, *, expected_status: str):
        current = self.values.get(value.tool_run_id)
        if current is None or current.current_status != expected_status:
            return None
        if self._store.fail_failure_update and value.current_status == "FAILED":
            return None
        self.values[value.tool_run_id] = value
        return value

    def bind(self, store: _Store) -> "_ToolRunRepository":
        self._store = store
        return self


class _Uow:
    def __init__(self, store: _Store) -> None:
        self.store = store
        self.tasks = _TaskRepository(store.tasks)
        self.task_input_revisions = _Repository(store.revisions)
        self.llm_calls = _Repository(store.llm_calls)
        self.tool_runs = _ToolRunRepository(store.tool_runs).bind(store)
        self.committed = False
        self._snapshots = {
            "tasks": dict(store.tasks),
            "revisions": dict(store.revisions),
            "llm_calls": dict(store.llm_calls),
            "tool_runs": dict(store.tool_runs),
        }

    def __enter__(self):
        self.store.active_uows += 1
        return self

    def __exit__(self, *_args: object) -> None:
        if not self.committed:
            for field_name, snapshot in self._snapshots.items():
                values = getattr(self.store, field_name)
                values.clear()
                values.update(snapshot)
        self.store.active_uows -= 1

    def commit(self) -> None:
        self.store.commit_count += 1
        if self.store.commit_count == self.store.fail_commit_at:
            raise PersistenceError("Persistence operation failed.")
        self.committed = True

    def rollback(self) -> None:
        pass


class _Client:
    def __init__(self, store: _Store, outcome: object) -> None:
        self.store = store
        self.outcome = outcome
        self.calls: list[object] = []

    def execute(self, metadata, validated_input, request_context):
        assert self.store.active_uows == 0
        self.calls.append((metadata, validated_input, request_context))
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome

    def readiness(self, _metadata) -> str:
        return "AVAILABLE"


def _success_output() -> ToolExecutionOutput:
    return ToolExecutionOutput(
        status="SUCCEEDED",
        requested_outputs=("sem_image", "mechanical_properties"),
        completed_outputs=("sem_image", "mechanical_properties"),
        failed_outputs=(),
        data={"mechanical_properties": {"yield_strength_mpa": 1000.0}},
        images=(
            ToolImagePayload(
                image_role="sem_image",
                requested_output=True,
                dtype="float32",
                numpy_dtype="<f4",
                shape=(512, 512),
                channel_layout="HW",
                value_range=(-1.0, 1.0),
                encoding="base64_npy",
                byte_order="little",
                array_order="C",
                sha256="a" * 64,
                data_base64="must-not-be-persisted",
            ),
        ),
        warnings=(),
        diagnostics=({"step": "mock_inference", "status": "SUCCEEDED"},),
        actual_runtime_parameters={
            "seed": 101,
            "num_samples": 1,
            "guide_scale": 2.0,
            "timesteps": 1000,
        },
        model_bundle_id="mock-zta35g-v1",
        error=None,
    )


def _service(
    outcome: object,
    *,
    task: Task | None = None,
    fail_commit_at: int | None = None,
    fail_failure_update: bool = False,
):
    store = _Store(
        tasks={"task_1": task or _task()},
        revisions={"revision_1": _revision()},
        llm_calls={"llm_1": _llm_call()},
        tool_runs={},
        fail_commit_at=fail_commit_at,
        fail_failure_update=fail_failure_update,
    )
    client = _Client(store, outcome)
    ids = iter(("tool_run_1", "tool_run_2"))
    seeds = iter((101, 202))
    ticks = count()
    service = ToolExecutionService(
        lambda: _Uow(store),
        build_tool_registry(client),
        clock=lambda: BASE + timedelta(seconds=2 + next(ticks)),
        id_factory=lambda: next(ids),
        seed_factory=lambda: next(seeds),
    )
    return service, store, client


def test_success_records_safe_pending_output_without_terminalizing_task() -> None:
    service, store, client = _service(_success_output())

    run = service.execute_revision(
        ActorContext(actor_id="actor_1", user_id=None),
        task_id="task_1",
        task_input_revision_id="revision_1",
        request_id="execute_request_1",
    )

    assert len(client.calls) == 1
    assert store.active_uows == 0
    assert run.current_status == "RUNNING"
    assert run.completed_at is None
    assert run.completed_outputs == []
    assert run.failed_outputs == []
    assert run.actual_runtime_parameters["seed"] == 101
    assert run.output_summary == {
        "runtime_status": "SUCCEEDED",
        "requested_outputs": ["sem_image", "mechanical_properties"],
        "completed_outputs": ["sem_image", "mechanical_properties"],
        "failed_outputs": [],
        "data_fields": ["mechanical_properties"],
        "image_count": 1,
        "image_roles": ["sem_image"],
        "images": [
            {
                "image_role": "sem_image",
                "requested_output": True,
                "sha256": "a" * 64,
                "encoding": "base64_npy",
                "shape": [512, 512],
            }
        ],
        "warning_count": 0,
        "error": None,
    }
    assert "must-not-be-persisted" not in repr(run)
    task = store.tasks["task_1"]
    assert (task.current_status, task.error_code) == ("FAILED", "TOOL_UNAVAILABLE")
    assert task.selected_tool_run_id is None
    assert task.selected_result_id is None


def test_internal_receipt_preserves_same_runtime_output_without_second_call() -> None:
    output = _success_output()
    service, _store, client = _service(output)

    receipt = service.execute_revision_with_output(
        ActorContext(actor_id="actor_1", user_id=None),
        task_id="task_1",
        task_input_revision_id="revision_1",
        request_id="execute_request_1",
    )

    assert receipt.output is output
    assert receipt.tool_run.output_summary["image_count"] == 1
    assert len(client.calls) == 1


@pytest.mark.parametrize("runtime_status", ["SUCCEEDED", "FAILED"])
def test_m7_activated_running_task_returns_normalized_receipt(
    runtime_status: str,
) -> None:
    activated_task = replace(
        _task(),
        current_status="RUNNING",
        completed_at=None,
        error_code=None,
        safe_error_message=None,
    )
    output = _success_output()
    if runtime_status == "FAILED":
        output = replace(
            output,
            status="FAILED",
            completed_outputs=(),
            failed_outputs=output.requested_outputs,
            data={},
            error={
                "code": "MECHANICAL_PROPERTY_PREDICTION_FAILED",
                "safe_message": "Runtime private wording.",
                "retryable": False,
            },
        )
    service, store, client = _service(output, task=activated_task)

    receipt = service.execute_revision_with_output(
        ActorContext(actor_id="actor_1", user_id=None),
        task_id="task_1",
        task_input_revision_id="revision_1",
        request_id="execute_request_1",
    )

    assert receipt.output.status == runtime_status
    assert receipt.tool_run.current_status == "RUNNING"
    assert receipt.tool_run.output_summary["runtime_status"] == runtime_status
    assert store.tasks["task_1"].current_status == "RUNNING"
    assert len(client.calls) == 1


@pytest.mark.parametrize(
    ("outcome", "expected_code", "expected_http"),
    [
        (ToolClientTimeoutError(), "RUNTIME_TIMEOUT", 504),
        (ToolClientUnavailableError(), "RUNTIME_UNAVAILABLE", 503),
        (ToolClientProtocolError(), "RUNTIME_PROTOCOL_ERROR", 502),
        (
            ToolClientRuntimeError(
                code="RUNTIME_BUSY",
                safe_message="Runtime is busy.",
                retryable=True,
            ),
            "RUNTIME_BUSY",
            503,
        ),
    ],
)
def test_failure_is_persisted_once_and_mapped_safely(
    outcome: Exception,
    expected_code: str,
    expected_http: int,
) -> None:
    service, store, client = _service(outcome)

    with pytest.raises(ToolExecutionOutcomeError) as raised:
        service.execute_revision(
            ActorContext(actor_id="actor_1", user_id=None),
            task_id="task_1",
            task_input_revision_id="revision_1",
            request_id="execute_request_1",
        )

    assert len(client.calls) == 1
    assert raised.value.code == expected_code
    assert raised.value.status_code == expected_http
    run = store.tool_runs[raised.value.tool_run_id]
    assert run.current_status == "FAILED"
    assert run.failed_outputs == ["sem_image", "mechanical_properties"]
    assert run.error_code == expected_code
    assert store.tasks["task_1"].error_code == "TOOL_UNAVAILABLE"


def test_failure_commit_error_returns_internal_error_and_leaves_run_running() -> None:
    service, store, client = _service(
        ToolClientTimeoutError(),
        fail_commit_at=3,
    )

    with pytest.raises(ApplicationInternalError) as raised:
        service.execute_revision(
            ActorContext(actor_id="actor_1", user_id=None),
            task_id="task_1",
            task_input_revision_id="revision_1",
            request_id="execute_request_1",
        )

    assert raised.value.code == "INTERNAL_ERROR"
    assert raised.value.status_code == 500
    assert len(client.calls) == 1
    assert len(store.tool_runs) == 1
    run = next(iter(store.tool_runs.values()))
    assert run.current_status == "RUNNING"
    assert run.error_code is None


def test_failure_update_none_returns_conflict_instead_of_runtime_error() -> None:
    service, store, client = _service(
        ToolClientTimeoutError(),
        fail_failure_update=True,
    )

    with pytest.raises(ApplicationConflictError) as raised:
        service.execute_revision(
            ActorContext(actor_id="actor_1", user_id=None),
            task_id="task_1",
            task_input_revision_id="revision_1",
            request_id="execute_request_1",
        )

    assert raised.value.code == "RESOURCE_CONFLICT"
    assert raised.value.status_code == 409
    assert len(client.calls) == 1
    run = next(iter(store.tool_runs.values()))
    assert run.current_status == "RUNNING"
    assert run.error_code is None


def test_runtime_error_text_is_replaced_by_stable_backend_message() -> None:
    service, store, client = _service(
        ToolClientRuntimeError(
            code="RUNTIME_BUSY",
            safe_message="Printable but Runtime-controlled text.",
            retryable=True,
        )
    )

    with pytest.raises(ToolExecutionOutcomeError) as raised:
        service.execute_revision(
            ActorContext(actor_id="actor_1", user_id=None),
            task_id="task_1",
            task_input_revision_id="revision_1",
            request_id="execute_request_1",
        )

    assert raised.value.code == "RUNTIME_BUSY"
    assert str(raised.value) == "Tool Runtime is busy."
    assert "Runtime-controlled" not in str(raised.value)
    assert len(client.calls) == 1
    run = next(iter(store.tool_runs.values()))
    assert run.current_status == "FAILED"
    assert run.safe_error_message == "Tool Runtime is busy."


def test_two_explicit_executions_use_new_tool_run_and_seed() -> None:
    service, store, client = _service(_success_output())
    actor = ActorContext(actor_id="actor_1", user_id=None)

    first = service.execute_revision(
        actor,
        task_id="task_1",
        task_input_revision_id="revision_1",
        request_id="execute_request_1",
    )
    second = service.execute_revision(
        actor,
        task_id="task_1",
        task_input_revision_id="revision_1",
        request_id="execute_request_2",
    )

    assert (first.tool_run_id, first.attempt_no) == ("tool_run_1", 1)
    assert (second.tool_run_id, second.attempt_no) == ("tool_run_2", 2)
    assert first.execution_input["runtime_parameters"]["seed"] == 101
    assert second.execution_input["runtime_parameters"]["seed"] == 202
    assert len(client.calls) == 2


@pytest.mark.parametrize(
    "task",
    [
        replace(_task(), task_type="KNOWLEDGE_QA"),
        replace(_task(), current_status="RUNNING"),
        replace(_task(), error_code="RUNTIME_TIMEOUT"),
        replace(_task(), selected_tool_run_id="selected_tool_run"),
        replace(
            _task(),
            selected_tool_run_id="selected_tool_run",
            selected_result_id="selected_result",
        ),
    ],
    ids=(
        "knowledge-task",
        "non-failed-task",
        "non-tool-unavailable-task",
        "selected-tool-run",
        "selected-result",
    ),
)
def test_only_m4_paused_tool_task_is_eligible_for_dev_execution(task: Task) -> None:
    service, store, client = _service(_success_output(), task=task)

    with pytest.raises(ApplicationConflictError):
        service.execute_revision(
            ActorContext(actor_id="actor_1", user_id=None),
            task_id="task_1",
            task_input_revision_id="revision_1",
            request_id="execute_request_1",
        )

    assert client.calls == []
    assert store.tool_runs == {}


def test_cross_task_revision_is_rejected_before_runtime_call() -> None:
    service, store, client = _service(_success_output())
    store.revisions["revision_1"] = _revision(task_id="different_task")

    with pytest.raises(ResourceNotFoundError):
        service.execute_revision(
            ActorContext(actor_id="actor_1", user_id=None),
            task_id="task_1",
            task_input_revision_id="revision_1",
            request_id="execute_request_1",
        )

    assert client.calls == []
    assert store.tool_runs == {}
