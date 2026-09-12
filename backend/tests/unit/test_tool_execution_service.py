from __future__ import annotations

from dataclasses import dataclass, field, replace
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
from materialsagent.application.tool_registry import ToolRegistry
from materialsagent.application.zta35g_tool import (
    build_zta35g_tool_definition,
)
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
from materialsagent.domain.ports.tool_registry import (
    AuthorizationDecision,
    ExecutionPolicy,
    ToolAction,
    ToolExecutionPolicy,
    ToolStatus,
)
from materialsagent.domain.ports.unit_of_work import PersistenceError


UTC = timezone.utc
BASE = datetime(2026, 7, 20, 8, 0, tzinfo=UTC)
SCHEMA_HASH = "f821240f782ce788bc723fd1acd02a2e58cedbf68b70b1414e2accd16d989d07"


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
        tool_id="zta35g_sem_virtual_lab",
        bound_tool_version="1",
        bound_schema_hash=SCHEMA_HASH,
    )


def _ready_task() -> Task:
    return replace(
        _task(),
        current_status="READY",
        completed_at=None,
        error_code=None,
        safe_error_message=None,
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
        source_message_id="message_1",
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
    idempotency_records: dict[str, object] = field(default_factory=dict)
    tool_results: dict[str, object] = field(default_factory=dict)
    active_uows: int = 0
    commit_count: int = 0
    fail_commit_at: int | None = None
    uncertain_commit_at: int | None = None
    uncertain_restore_task: bool = False
    uncertain_drop_run: bool = False
    fail_failure_update: bool = False
    task_lock_reads: int = 0
    runtime_call_count: int = 0
    commit_observations: list[dict[str, object]] = field(default_factory=list)


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
                "idempotency_record_id",
                "result_id",
            )
            if hasattr(value, field)
        )
        self.values[resource_id] = value


class _TaskRepository(_Repository):
    def bind(self, store: _Store) -> "_TaskRepository":
        self._store = store
        return self

    def get_owned(self, task_id: str, actor_id: str):
        value = self.get(task_id)
        return value if value is not None and value.actor_id == actor_id else None

    def get_owned_for_update(self, task_id: str, actor_id: str):
        self._store.task_lock_reads += 1
        return self.get_owned(task_id, actor_id)

    def update(self, value: Task, *, expected_status: str):
        current = self.values.get(value.task_id)
        if current is None or current.current_status != expected_status:
            return None
        self.values[value.task_id] = value
        return value


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


class _TaskInputRevisionRepository(_Repository):
    def list_for_task(self, task_id: str):
        return sorted(
            (value for value in self.values.values() if value.task_id == task_id),
            key=lambda item: item.revision,
        )


class _IdempotencyRepository(_Repository):
    def get_by_scope(self, actor_id: str, operation: str, idempotency_key: str):
        return next(
            (
                value
                for value in self.values.values()
                if value.actor_id == actor_id
                and value.operation == operation
                and value.idempotency_key == idempotency_key
            ),
            None,
        )


class _ToolResultRepository(_Repository):
    def get_for_tool_run(self, tool_run_id: str):
        return next(
            (
                value
                for value in self.values.values()
                if value.tool_run_id == tool_run_id
            ),
            None,
        )


class _Uow:
    def __init__(self, store: _Store) -> None:
        self.store = store
        self.tasks = _TaskRepository(store.tasks).bind(store)
        self.task_input_revisions = _TaskInputRevisionRepository(store.revisions)
        self.llm_calls = _Repository(store.llm_calls)
        self.tool_runs = _ToolRunRepository(store.tool_runs).bind(store)
        self.idempotency_records = _IdempotencyRepository(
            store.idempotency_records
        )
        self.tool_results = _ToolResultRepository(store.tool_results)
        self.committed = False
        self._snapshots = {
            "tasks": dict(store.tasks),
            "revisions": dict(store.revisions),
            "llm_calls": dict(store.llm_calls),
            "tool_runs": dict(store.tool_runs),
            "idempotency_records": dict(store.idempotency_records),
            "tool_results": dict(store.tool_results),
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
        self.store.commit_observations.append(
            {
                "commit_no": self.store.commit_count,
                "task_status": self.store.tasks["task_1"].current_status,
                "run_statuses": {
                    run_id: run.current_status
                    for run_id, run in self.store.tool_runs.items()
                },
                "runtime_calls": self.store.runtime_call_count,
            }
        )
        if self.store.commit_count == self.store.fail_commit_at:
            raise PersistenceError("Persistence operation failed.")
        self.committed = True
        if self.store.commit_count == self.store.uncertain_commit_at:
            if self.store.uncertain_restore_task:
                self.store.tasks["task_1"] = self._snapshots["tasks"][
                    "task_1"
                ]
            if self.store.uncertain_drop_run:
                self.store.tool_runs.pop("tool_run_1", None)
            raise PersistenceError("Commit outcome was uncertain.")

    def rollback(self) -> None:
        pass


class _Client:
    def __init__(self, store: _Store, outcome: object) -> None:
        self.store = store
        self.outcome = outcome
        self.calls: list[object] = []

    def execute(self, metadata, validated_input, request_context):
        assert self.store.active_uows == 0
        self.store.runtime_call_count += 1
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
    uncertain_commit_at: int | None = None,
    uncertain_restore_task: bool = False,
    uncertain_drop_run: bool = False,
    fail_failure_update: bool = False,
    execution_policy: ExecutionPolicy = ExecutionPolicy.ANY_TASK,
    registration_version: str = "1",
    registration_input_schema: dict[str, object] | None = None,
    registry_wrapper=None,
    seed_factory=None,
):
    store = _Store(
        tasks={"task_1": task or _ready_task()},
        revisions={"revision_1": _revision()},
        llm_calls={"llm_1": _llm_call()},
        tool_runs={},
        fail_commit_at=fail_commit_at,
        uncertain_commit_at=uncertain_commit_at,
        uncertain_restore_task=uncertain_restore_task,
        uncertain_drop_run=uncertain_drop_run,
        fail_failure_update=fail_failure_update,
    )
    client = _Client(store, outcome)
    ids = iter(("tool_run_1", "tool_run_2"))
    seeds = iter((101, 202))
    ticks = count()
    status = (
        ToolStatus.ACTIVE
        if execution_policy is ExecutionPolicy.ANY_TASK
        else ToolStatus.DEPRECATED
        if execution_policy is ExecutionPolicy.EXISTING_TASK_ONLY
        else ToolStatus.DISABLED
    )
    registration = build_zta35g_tool_definition(client)
    registration = replace(
        registration,
        definition=replace(
            registration.definition,
            version=registration_version,
            status=status,
            tool_execution_policy=ToolExecutionPolicy(
                lifecycle_policy=execution_policy,
            ),
            **(
                {}
                if registration_input_schema is None
                else {"input_schema": registration_input_schema}
            ),
        ),
    )
    registry = ToolRegistry((registration,))
    if registry_wrapper is not None:
        registry = registry_wrapper(registry)
    service = ToolExecutionService(
        lambda: _Uow(store),
        registry,
        clock=lambda: BASE + timedelta(seconds=2 + next(ticks)),
        id_factory=lambda: next(ids),
        seed_factory=(lambda: next(seeds)) if seed_factory is None else seed_factory,
    )
    return service, store, client


class _DecisionPolicyRegistry:
    def __init__(
        self,
        registry: ToolRegistry,
        policy: ExecutionPolicy,
    ) -> None:
        self._registry = registry
        self._policy = policy

    def resolve(self, tool_id: str):
        return self._registry.resolve(tool_id)

    def authorize(self, *, registration, action, bound_ref):
        assert action in {ToolAction.EXECUTE, ToolAction.RETRY}
        assert bound_ref is not None
        return AuthorizationDecision(
            authorized_ref=registration.ref,
            execution_policy_snapshot=self._policy,
        )


class _SwitchableRegistry:
    def __init__(self, registry: ToolRegistry) -> None:
        self._registry = registry

    def replace_with(self, registry: ToolRegistry) -> None:
        self._registry = registry

    def resolve(self, tool_id: str):
        return self._registry.resolve(tool_id)

    def authorize(self, *, registration, action, bound_ref):
        return self._registry.authorize(
            registration=registration,
            action=action,
            bound_ref=bound_ref,
        )


def _current_registry(
    client: _Client,
    *,
    execution_policy: ExecutionPolicy = ExecutionPolicy.ANY_TASK,
    version: str = "1",
    input_schema: dict[str, object] | None = None,
) -> ToolRegistry:
    status = (
        ToolStatus.ACTIVE
        if execution_policy is ExecutionPolicy.ANY_TASK
        else ToolStatus.DEPRECATED
        if execution_policy is ExecutionPolicy.EXISTING_TASK_ONLY
        else ToolStatus.DISABLED
    )
    registration = build_zta35g_tool_definition(client)
    registration = replace(
        registration,
        definition=replace(
            registration.definition,
            version=version,
            status=status,
            tool_execution_policy=ToolExecutionPolicy(
                lifecycle_policy=execution_policy,
            ),
            **({} if input_schema is None else {"input_schema": input_schema}),
        ),
    )
    return ToolRegistry((registration,))


def _failed_run(*, attempt_no: int = 1, seed: int = 101):
    from materialsagent.domain.models.tool_run import ToolRun

    pending = ToolRun.pending(
        tool_run_id="old_run",
        task_id="task_1",
        request_id="old_request",
        task_input_revision_id="revision_1",
        input_revision_no=1,
        attempt_no=attempt_no,
        tool_id="zta35g_sem_virtual_lab",
        tool_version="1",
        schema_hash=SCHEMA_HASH,
        normalized_input_snapshot=dict(_revision().normalized_input),
        execution_policy_snapshot=ExecutionPolicy.ANY_TASK,
        execution_input={
            "material": "ZTA35G",
            "requested_outputs": ["sem_image", "mechanical_properties"],
            "runtime_parameters": {
                "seed": seed,
                "num_samples": 1,
                "guide_scale": 2.0,
                "timesteps": 1000,
            },
        },
        requested_outputs=["sem_image", "mechanical_properties"],
        created_at=BASE,
    )
    return pending.start(started_at=BASE).fail(
        failed_at=BASE + timedelta(seconds=1),
        error_code="RUNTIME_UNAVAILABLE",
        safe_error_message="Runtime unavailable.",
    )


def test_ready_task_commits_running_task_with_pending_run_before_runtime() -> None:
    service, store, client = _service(
        _success_output(),
        task=_ready_task(),
    )

    receipt = service.execute_revision_with_output(
        ActorContext(actor_id="actor_1", user_id=None),
        task_id="task_1",
        task_input_revision_id="revision_1",
        request_id="execute_request_1",
    )

    assert store.task_lock_reads == 1
    assert store.commit_observations[0] == {
        "commit_no": 1,
        "task_status": "RUNNING",
        "run_statuses": {"tool_run_1": "PENDING"},
        "runtime_calls": 0,
    }
    assert client.calls
    assert store.commit_observations[1]["run_statuses"] == {
        "tool_run_1": "RUNNING"
    }
    assert store.tasks["task_1"].current_status == "RUNNING"
    assert receipt.tool_run.current_status == "RUNNING"


def test_ready_task_authorization_denial_has_zero_persistent_effects() -> None:
    ready = _ready_task()
    service, store, client = _service(
        _success_output(),
        task=ready,
        execution_policy=ExecutionPolicy.NONE,
    )

    with pytest.raises(ApplicationConflictError):
        service.execute_revision_with_output(
            ActorContext(actor_id="actor_1", user_id=None),
            task_id="task_1",
            task_input_revision_id="revision_1",
            request_id="execute_request_1",
        )

    assert store.tasks["task_1"] == ready
    assert store.tool_runs == {}
    assert store.commit_count == 0
    assert client.calls == []


def test_compatible_current_version_execution_uses_current_ref_without_rebinding() -> None:
    ready = _ready_task()
    service, store, _client = _service(
        _success_output(),
        task=ready,
        execution_policy=ExecutionPolicy.EXISTING_TASK_ONLY,
        registration_version="2",
    )

    receipt = service.execute_revision_with_output(
        ActorContext(actor_id="actor_1", user_id=None),
        task_id="task_1",
        task_input_revision_id="revision_1",
        request_id="execute_request_v2",
    )

    assert receipt.tool_run.tool_version == "2"
    assert receipt.tool_run.schema_hash == ready.bound_schema_hash
    assert (
        receipt.tool_run.execution_policy_snapshot
        is ExecutionPolicy.EXISTING_TASK_ONLY
    )
    assert store.tasks["task_1"].bound_tool_version == "1"


def test_execution_persists_policy_only_from_authorization_decision() -> None:
    service, _store, _client = _service(
        _success_output(),
        task=_ready_task(),
        registry_wrapper=lambda registry: _DecisionPolicyRegistry(
            registry,
            ExecutionPolicy.EXISTING_TASK_ONLY,
        ),
    )

    receipt = service.execute_revision_with_output(
        ActorContext(actor_id="actor_1", user_id=None),
        task_id="task_1",
        task_input_revision_id="revision_1",
        request_id="execute_request_policy_snapshot",
    )

    assert (
        receipt.tool_run.execution_policy_snapshot
        is ExecutionPolicy.EXISTING_TASK_ONLY
    )


def test_schema_drift_denies_execution_before_seed_validation_or_pending_run() -> None:
    seed_calls: list[None] = []

    def forbidden_seed() -> int:
        seed_calls.append(None)
        return 101

    service, store, client = _service(
        _success_output(),
        task=_ready_task(),
        registration_input_schema={
            "type": "object",
            "properties": {"changed": {"type": "string"}},
        },
        seed_factory=forbidden_seed,
    )

    with pytest.raises(ApplicationConflictError) as raised:
        service.execute_revision_with_output(
            ActorContext(actor_id="actor_1", user_id=None),
            task_id="task_1",
            task_input_revision_id="revision_1",
            request_id="execute_request_schema_drift",
        )

    assert raised.value.code == "TOOL_SCHEMA_DRIFT"
    assert "新任务" in str(raised.value)
    assert seed_calls == []
    assert store.tool_runs == {}
    assert store.commit_count == 0
    assert client.calls == []


def test_retry_reauthorizes_current_version_and_keeps_old_attempt_immutable() -> None:
    task = replace(_task(), selected_tool_run_id="old_run")
    service, store, client = _service(
        _success_output(),
        task=task,
        execution_policy=ExecutionPolicy.EXISTING_TASK_ONLY,
        registration_version="2",
        seed_factory=lambda: 202,
    )
    old_run = _failed_run()
    store.tool_runs[old_run.tool_run_id] = old_run
    old_snapshot = replace(old_run)

    reservation = service.reserve_retry_attempt(
        ActorContext(actor_id="actor_1", user_id=None),
        task_id="task_1",
        request_id="retry_request_v2",
        idempotency_key="retry-v2",
        request_digest="a" * 64,
    )

    new_run = reservation.tool_run
    assert store.tool_runs["old_run"] == old_snapshot
    assert new_run.tool_run_id == "tool_run_1"
    assert new_run.attempt_no == 2
    assert new_run.execution_input["runtime_parameters"]["seed"] == 202
    assert new_run.tool_version == "2"
    assert new_run.schema_hash == SCHEMA_HASH
    assert (
        new_run.execution_policy_snapshot
        is ExecutionPolicy.EXISTING_TASK_ONLY
    )
    assert store.tasks["task_1"].bound_tool_version == "1"
    assert client.calls == []


@pytest.mark.parametrize(
    ("execution_policy", "expected_code"),
    [
        (ExecutionPolicy.ANY_TASK, "TOOL_SCHEMA_DRIFT"),
        (ExecutionPolicy.NONE, "TOOL_EXECUTION_NOT_ALLOWED"),
    ],
)
def test_retry_denial_preserves_old_attempt_and_allocates_no_seed_or_pending_run(
    execution_policy: ExecutionPolicy,
    expected_code: str,
) -> None:
    seed_calls: list[None] = []

    def forbidden_seed() -> int:
        seed_calls.append(None)
        return 202

    task = replace(_task(), selected_tool_run_id="old_run")
    service, store, client = _service(
        _success_output(),
        task=task,
        execution_policy=execution_policy,
        registration_input_schema=(
            {
                "type": "object",
                "properties": {"changed": {"type": "string"}},
            }
            if execution_policy is ExecutionPolicy.ANY_TASK
            else None
        ),
        seed_factory=forbidden_seed,
    )
    old_run = _failed_run()
    store.tool_runs[old_run.tool_run_id] = old_run
    old_snapshot = replace(old_run)

    with pytest.raises(ApplicationConflictError) as raised:
        service.reserve_retry_attempt(
            ActorContext(actor_id="actor_1", user_id=None),
            task_id="task_1",
            request_id="retry_request_denied",
            idempotency_key="retry-denied",
            request_digest="b" * 64,
        )

    assert raised.value.code == expected_code
    if expected_code == "TOOL_SCHEMA_DRIFT":
        assert "新任务" in str(raised.value)
    assert seed_calls == []
    assert store.tool_runs == {"old_run": old_snapshot}
    assert store.idempotency_records == {}
    assert store.commit_count == 0
    assert client.calls == []


def test_competing_retry_with_different_key_conflicts_without_partial_attempt() -> None:
    task = replace(_task(), selected_tool_run_id="old_run")
    service, store, client = _service(
        _success_output(),
        task=task,
        seed_factory=lambda: 202,
    )
    old_run = _failed_run()
    store.tool_runs[old_run.tool_run_id] = old_run
    actor = ActorContext(actor_id="actor_1", user_id=None)

    winner = service.reserve_retry_attempt(
        actor,
        task_id=task.task_id,
        request_id="retry_request_winner",
        idempotency_key="retry-key-winner",
        request_digest="a" * 64,
    )

    with pytest.raises(ApplicationConflictError) as raised:
        service.reserve_retry_attempt(
            actor,
            task_id=task.task_id,
            request_id="retry_request_loser",
            idempotency_key="retry-key-loser",
            request_digest="b" * 64,
        )

    assert raised.value.code == "TASK_NOT_RETRYABLE"
    assert winner.idempotency_outcome.value == "CREATED"
    assert winner.tool_run.tool_run_id == "tool_run_1"
    assert winner.tool_run.attempt_no == 2
    assert set(store.tool_runs) == {"old_run", "tool_run_1"}
    assert store.tool_runs["old_run"] == old_run
    assert len(store.idempotency_records) == 1
    only_record = next(iter(store.idempotency_records.values()))
    assert only_record.idempotency_key == "retry-key-winner"
    assert only_record.tool_run_id == "tool_run_1"
    assert store.commit_count == 1
    assert store.task_lock_reads == 2
    assert store.tasks[task.task_id].bound_tool_ref == task.bound_tool_ref
    assert store.tasks[task.task_id].current_status == "RUNNING"
    assert client.calls == []


def test_competing_retry_with_same_key_replays_one_attempt_identity() -> None:
    task = replace(_task(), selected_tool_run_id="old_run")
    service, store, client = _service(
        _success_output(),
        task=task,
        seed_factory=lambda: 202,
    )
    old_run = _failed_run()
    store.tool_runs[old_run.tool_run_id] = old_run
    actor = ActorContext(actor_id="actor_1", user_id=None)

    winner = service.reserve_retry_attempt(
        actor,
        task_id=task.task_id,
        request_id="retry_request_winner",
        idempotency_key="retry-shared-key",
        request_digest="c" * 64,
    )
    replay = service.reserve_retry_attempt(
        actor,
        task_id=task.task_id,
        request_id="retry_request_replay",
        idempotency_key="retry-shared-key",
        request_digest="c" * 64,
    )

    assert winner.idempotency_outcome.value == "CREATED"
    assert replay.idempotency_outcome.value == "REPLAY"
    assert replay.tool_run == winner.tool_run
    assert replay.tool_run.tool_run_id == "tool_run_1"
    assert replay.tool_run.attempt_no == 2
    assert set(store.tool_runs) == {"old_run", "tool_run_1"}
    assert store.tool_runs["old_run"] == old_run
    assert len(store.idempotency_records) == 1
    assert store.commit_count == 1
    assert store.task_lock_reads == 1
    assert store.tasks[task.task_id].bound_tool_ref == task.bound_tool_ref
    assert client.calls == []


def test_competing_execution_of_one_reserved_retry_has_one_runtime_invoker() -> None:
    task = replace(_task(), selected_tool_run_id="old_run")
    service, store, client = _service(
        _success_output(),
        task=task,
        seed_factory=lambda: 202,
    )
    old_run = _failed_run()
    store.tool_runs[old_run.tool_run_id] = old_run
    actor = ActorContext(actor_id="actor_1", user_id=None)
    reservation = service.reserve_retry_attempt(
        actor,
        task_id=task.task_id,
        request_id="retry_execute_winner",
        idempotency_key="retry-execute-key",
        request_digest="d" * 64,
    )
    runtime_calls = 0
    competing_errors: list[ApplicationConflictError] = []

    def interleaving_execute(_metadata, validated_input, _request_context):
        nonlocal runtime_calls
        runtime_calls += 1
        if runtime_calls == 1:
            try:
                service.execute_reserved_retry(
                    actor,
                    tool_run_id=reservation.tool_run.tool_run_id,
                )
            except ApplicationConflictError as error:
                competing_errors.append(error)
        return replace(
            _success_output(),
            actual_runtime_parameters=dict(validated_input.runtime_parameters),
        )

    client.execute = interleaving_execute

    receipt = service.execute_reserved_retry(
        actor,
        tool_run_id=reservation.tool_run.tool_run_id,
    )

    assert receipt is not None
    assert receipt.tool_run.tool_run_id == "tool_run_1"
    assert receipt.tool_run.attempt_no == 2
    assert [error.code for error in competing_errors] == ["RESOURCE_CONFLICT"]
    assert runtime_calls == 1
    assert set(store.tool_runs) == {"old_run", "tool_run_1"}
    assert store.tool_runs["old_run"] == old_run
    assert len(store.idempotency_records) == 1
    assert store.tasks[task.task_id].bound_tool_ref == task.bound_tool_ref


@pytest.mark.parametrize(
    ("execution_policy", "input_schema", "expected_code"),
    [
        (ExecutionPolicy.NONE, None, "TOOL_EXECUTION_NOT_ALLOWED"),
        (
            ExecutionPolicy.ANY_TASK,
            {
                "type": "object",
                "properties": {"changed": {"type": "string"}},
            },
            "TOOL_SCHEMA_DRIFT",
        ),
    ],
)
def test_reserved_retry_reauthorizes_before_start_and_preserves_pending_replay(
    execution_policy: ExecutionPolicy,
    input_schema: dict[str, object] | None,
    expected_code: str,
) -> None:
    holder: dict[str, _SwitchableRegistry] = {}

    def switchable(registry: ToolRegistry) -> _SwitchableRegistry:
        wrapped = _SwitchableRegistry(registry)
        holder["registry"] = wrapped
        return wrapped

    task = replace(_task(), selected_tool_run_id="old_run")
    service, store, client = _service(
        _success_output(),
        task=task,
        registry_wrapper=switchable,
        seed_factory=lambda: 202,
    )
    old_run = _failed_run()
    store.tool_runs[old_run.tool_run_id] = old_run
    actor = ActorContext(actor_id="actor_1", user_id=None)
    reservation = service.reserve_retry_attempt(
        actor,
        task_id=task.task_id,
        request_id="retry_authorization_reservation",
        idempotency_key="retry-authorization-key",
        request_digest="e" * 64,
    )
    pending_before = replace(reservation.tool_run)
    task_before = store.tasks[task.task_id]
    records_before = dict(store.idempotency_records)
    commits_before = store.commit_count
    holder["registry"].replace_with(
        _current_registry(
            client,
            execution_policy=execution_policy,
            input_schema=input_schema,
        )
    )

    with pytest.raises(ApplicationConflictError) as raised:
        service.execute_reserved_retry(
            actor,
            tool_run_id=reservation.tool_run.tool_run_id,
        )

    replay = service.reserve_retry_attempt(
        actor,
        task_id=task.task_id,
        request_id="retry_authorization_replay",
        idempotency_key="retry-authorization-key",
        request_digest="e" * 64,
    )
    assert raised.value.code == expected_code
    assert replay.idempotency_replayed is True
    assert replay.tool_run == pending_before
    assert store.tool_runs[pending_before.tool_run_id] == pending_before
    assert store.tool_runs[pending_before.tool_run_id].current_status == "PENDING"
    assert store.tool_runs[pending_before.tool_run_id].tool_version == "1"
    assert (
        store.tool_runs[pending_before.tool_run_id].execution_policy_snapshot
        is ExecutionPolicy.ANY_TASK
    )
    assert store.tasks[task.task_id] == task_before
    assert store.idempotency_records == records_before
    assert store.commit_count == commits_before
    assert client.calls == []


def test_reserved_retry_rejects_compatible_current_version_before_start() -> None:
    holder: dict[str, _SwitchableRegistry] = {}

    def switchable(registry: ToolRegistry) -> _SwitchableRegistry:
        wrapped = _SwitchableRegistry(registry)
        holder["registry"] = wrapped
        return wrapped

    task = replace(_task(), selected_tool_run_id="old_run")
    service, store, client = _service(
        _success_output(),
        task=task,
        registry_wrapper=switchable,
        seed_factory=lambda: 202,
    )
    store.tool_runs["old_run"] = _failed_run()
    actor = ActorContext(actor_id="actor_1", user_id=None)
    reservation = service.reserve_retry_attempt(
        actor,
        task_id=task.task_id,
        request_id="retry_version_reservation",
        idempotency_key="retry-version-key",
        request_digest="f" * 64,
    )
    pending_before = replace(reservation.tool_run)
    commits_before = store.commit_count
    holder["registry"].replace_with(
        _current_registry(client, version="2")
    )

    with pytest.raises(ApplicationConflictError) as raised:
        service.execute_reserved_retry(
            actor,
            tool_run_id=reservation.tool_run.tool_run_id,
        )

    assert raised.value.code == "TOOL_EXECUTION_NOT_ALLOWED"
    assert store.tool_runs[pending_before.tool_run_id] == pending_before
    assert store.tool_runs[pending_before.tool_run_id].current_status == "PENDING"
    assert store.tool_runs[pending_before.tool_run_id].tool_version == "1"
    assert store.commit_count == commits_before
    assert client.calls == []


def test_ready_uncertain_commit_recovers_only_exact_run_and_running_task() -> None:
    service, store, client = _service(
        _success_output(),
        task=_ready_task(),
        uncertain_commit_at=1,
    )

    receipt = service.execute_revision_with_output(
        ActorContext(actor_id="actor_1", user_id=None),
        task_id="task_1",
        task_input_revision_id="revision_1",
        request_id="execute_request_1",
    )

    assert receipt.tool_run.tool_run_id == "tool_run_1"
    assert store.tasks["task_1"].current_status == "RUNNING"
    assert len(client.calls) == 1


def test_ready_uncertain_commit_rejects_pending_run_with_ready_task() -> None:
    service, store, client = _service(
        _success_output(),
        task=_ready_task(),
        uncertain_commit_at=1,
        uncertain_restore_task=True,
    )

    with pytest.raises(ApplicationConflictError):
        service.execute_revision_with_output(
            ActorContext(actor_id="actor_1", user_id=None),
            task_id="task_1",
            task_input_revision_id="revision_1",
            request_id="execute_request_1",
        )

    assert store.tasks["task_1"].current_status == "READY"
    assert store.tool_runs["tool_run_1"].current_status == "PENDING"
    assert client.calls == []


def test_ready_uncertain_commit_rejects_running_task_without_pending_run() -> None:
    service, store, client = _service(
        _success_output(),
        task=_ready_task(),
        uncertain_commit_at=1,
        uncertain_drop_run=True,
    )

    with pytest.raises(ApplicationConflictError) as raised:
        service.execute_revision_with_output(
            ActorContext(actor_id="actor_1", user_id=None),
            task_id="task_1",
            task_input_revision_id="revision_1",
            request_id="execute_request_1",
        )

    assert raised.value.code == "RESOURCE_CONFLICT"
    assert store.tasks["task_1"].current_status == "RUNNING"
    assert store.tool_runs == {}
    assert client.calls == []


def test_ready_uncertain_commit_exact_rollback_maps_persistence_error() -> None:
    ready = _ready_task()
    service, store, client = _service(
        _success_output(),
        task=ready,
        uncertain_commit_at=1,
        uncertain_restore_task=True,
        uncertain_drop_run=True,
    )

    with pytest.raises(ApplicationInternalError) as raised:
        service.execute_revision_with_output(
            ActorContext(actor_id="actor_1", user_id=None),
            task_id="task_1",
            task_input_revision_id="revision_1",
            request_id="execute_request_1",
        )

    assert raised.value.code == "INTERNAL_ERROR"
    assert raised.value.status_code == 500
    assert store.tasks["task_1"] == ready
    assert store.tool_runs == {}
    assert client.calls == []


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
    assert (task.current_status, task.error_code) == ("RUNNING", None)
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
    "summary",
    [
        {
            "route": "TOOL_EXECUTION",
            "tool_id": "zta35g_sem_virtual_lab",
        },
        {
            "route": "TOOL_CANDIDATES",
            "candidates": [
                {
                    "tool_id": "zta35g_sem_virtual_lab",
                    "candidate_input": {},
                }
            ],
        },
    ],
)
def test_unbound_task_cannot_execute_from_old_or_new_llm_summary(
    summary: dict[str, object],
) -> None:
    unbound = replace(
        _task(),
        current_status="RUNNING",
        completed_at=None,
        error_code=None,
        safe_error_message=None,
        tool_id=None,
        bound_tool_version=None,
        bound_schema_hash=None,
    )
    service, store, client = _service(_success_output(), task=unbound)
    audit = (
        {
            "catalog_snapshot_refs": (
                {
                    "tool_id": "zta35g_sem_virtual_lab",
                    "version": "1",
                    "schema_hash": SCHEMA_HASH,
                },
            ),
            "catalog_hash": "a" * 64,
        }
        if summary["route"] == "TOOL_CANDIDATES"
        else {}
    )
    store.llm_calls["llm_1"] = replace(
        _llm_call(),
        structured_output_summary=summary,
        **audit,
    )

    with pytest.raises(ResourceNotFoundError):
        service.execute_revision_with_output(
            ActorContext(actor_id="actor_1", user_id=None),
            task_id="task_1",
            task_input_revision_id="revision_1",
            request_id="execute_request_1",
        )

    assert client.calls == []
    assert store.tool_runs == {}


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
    assert store.tasks["task_1"].error_code is None


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


def test_second_explicit_execution_is_rejected_while_first_run_is_active() -> None:
    service, store, client = _service(_success_output())
    actor = ActorContext(actor_id="actor_1", user_id=None)

    first = service.execute_revision(
        actor,
        task_id="task_1",
        task_input_revision_id="revision_1",
        request_id="execute_request_1",
    )
    commit_count_after_first = store.commit_count

    with pytest.raises(ApplicationConflictError) as raised:
        service.execute_revision(
            actor,
            task_id="task_1",
            task_input_revision_id="revision_1",
            request_id="execute_request_2",
        )

    assert raised.value.code == "RESOURCE_CONFLICT"
    assert (first.tool_run_id, first.attempt_no) == ("tool_run_1", 1)
    assert first.execution_input["runtime_parameters"]["seed"] == 101
    assert set(store.tool_runs) == {"tool_run_1"}
    assert store.commit_count == commit_count_after_first
    assert len(client.calls) == 1


def test_two_failed_public_initial_calls_create_only_attempt_one() -> None:
    service, store, client = _service(ToolClientUnavailableError())
    actor = ActorContext(actor_id="actor_1", user_id=None)

    with pytest.raises(ToolExecutionOutcomeError):
        service.execute_revision(
            actor,
            task_id="task_1",
            task_input_revision_id="revision_1",
            request_id="failed_initial_request_1",
        )
    failed_attempt = store.tool_runs["tool_run_1"]
    commits_after_first = store.commit_count

    with pytest.raises(ApplicationConflictError) as raised:
        service.execute_revision(
            actor,
            task_id="task_1",
            task_input_revision_id="revision_1",
            request_id="failed_initial_request_2",
        )

    assert raised.value.code == "RESOURCE_CONFLICT"
    assert store.tool_runs == {"tool_run_1": failed_attempt}
    assert failed_attempt.current_status == "FAILED"
    assert failed_attempt.attempt_no == 1
    assert store.commit_count == commits_after_first
    assert len(client.calls) == 1


@pytest.mark.parametrize("active_status", ["PENDING", "RUNNING"])
def test_initial_execute_rejects_preexisting_active_attempt(
    active_status: str,
) -> None:
    from materialsagent.domain.models.tool_run import ToolRun

    service, store, client = _service(_success_output())
    pending = ToolRun.pending(
        tool_run_id="active_run",
        task_id="task_1",
        request_id="active_request",
        task_input_revision_id="revision_1",
        input_revision_no=1,
        attempt_no=1,
        tool_id="zta35g_sem_virtual_lab",
        tool_version="1",
        schema_hash=SCHEMA_HASH,
        normalized_input_snapshot=dict(_revision().normalized_input),
        execution_policy_snapshot=ExecutionPolicy.ANY_TASK,
        execution_input={
            "material": "ZTA35G",
            "requested_outputs": ["sem_image", "mechanical_properties"],
            "runtime_parameters": {
                "seed": 101,
                "num_samples": 1,
                "guide_scale": 2.0,
                "timesteps": 1000,
            },
        },
        requested_outputs=["sem_image", "mechanical_properties"],
        created_at=BASE,
    )
    active = (
        pending
        if active_status == "PENDING"
        else pending.start(started_at=BASE + timedelta(seconds=1))
    )
    store.tool_runs[active.tool_run_id] = active
    task_before = store.tasks["task_1"]

    with pytest.raises(ApplicationConflictError) as raised:
        service.execute_revision(
            ActorContext(actor_id="actor_1", user_id=None),
            task_id="task_1",
            task_input_revision_id="revision_1",
            request_id="execute_request_loser",
        )

    assert raised.value.code == "RESOURCE_CONFLICT"
    assert store.tool_runs == {"active_run": active}
    assert store.tasks["task_1"] == task_before
    assert store.commit_count == 0
    assert client.calls == []


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


def test_generic_execution_dispatches_ml_fixture_with_immutable_provenance() -> None:
    from backend.tests.support.heterogeneous_tools import (
        build_ml_training_test_definition,
    )

    class ForbiddenZtaClient:
        def execute(self, *_args, **_kwargs):
            pytest.fail("The ML ToolRun invoked the ZTA35G Runtime client.")

        def readiness(self, _metadata):
            pytest.fail("The ML ToolRun queried ZTA35G readiness.")

    original_ml = ToolRegistry(
        (build_ml_training_test_definition(),)
    ).resolve("ml_training_test")
    current_ml = build_ml_training_test_definition(version="2")
    registry = ToolRegistry(
        (
            build_zta35g_tool_definition(ForbiddenZtaClient()),
            current_ml,
        )
    )
    current_ml = registry.resolve("ml_training_test")
    ready = replace(
        _ready_task(),
        tool_id=original_ml.tool_id,
        bound_tool_version=original_ml.version,
        bound_schema_hash=original_ml.schema_hash,
    )
    normalized_ml = {
        "dataset": "dataset_fixture_1",
        "task_type": "regression",
        "split_ratio": 0.8,
        "target_column": "yield_strength",
        "shuffle": True,
    }
    revision = replace(
        _revision(),
        raw_input=dict(normalized_ml),
        normalized_input=dict(normalized_ml),
    )
    llm_call = replace(
        _llm_call(),
        structured_output_summary={
            "route": "TOOL_CANDIDATES",
            "candidates": [
                {
                    "tool_id": "ml_training_test",
                    "candidate_input": dict(normalized_ml),
                }
            ],
        },
        catalog_snapshot_refs=(original_ml.ref,),
        catalog_hash="c" * 64,
    )
    store = _Store(
        tasks={ready.task_id: ready},
        revisions={revision.task_input_revision_id: revision},
        llm_calls={llm_call.llm_call_id: llm_call},
        tool_runs={},
    )
    ticks = count()
    service = ToolExecutionService(
        lambda: _Uow(store),
        registry,
        clock=lambda: BASE + timedelta(seconds=10 + next(ticks)),
        id_factory=lambda: "ml_tool_run_1",
        seed_factory=lambda: 17,
    )

    receipt = service.execute_revision_with_output(
        ActorContext(actor_id="actor_1", user_id=None),
        task_id=ready.task_id,
        task_input_revision_id=revision.task_input_revision_id,
        request_id="execute_ml_request_1",
    )

    run = receipt.tool_run
    assert run.tool_id == "ml_training_test"
    assert run.tool_version == "2"
    assert run.schema_hash == original_ml.schema_hash == current_ml.schema_hash
    assert run.execution_policy_snapshot is ExecutionPolicy.ANY_TASK
    assert run.normalized_input_snapshot == normalized_ml
    assert run.execution_input == {
        "process_parameters": normalized_ml,
        "requested_outputs": ["training_metrics"],
        "runtime_parameters": {"seed": 17},
    }
    assert run.input_revision_no == 1
    assert run.attempt_no == 1
    assert store.tasks[ready.task_id].bound_tool_version == "1"
    assert receipt.output.data == {
        "training_metrics": {
            "fixture_score": 0.91,
            "fixture_only": True,
        }
    }
    assert current_ml.tool.validation_calls == [normalized_ml]
    assert len(current_ml.tool.execution_calls) == 1


def test_competing_execute_cannot_create_a_second_pending_attempt() -> None:
    class InterleavingClient:
        def __init__(self) -> None:
            self.service: ToolExecutionService | None = None
            self.calls = 0
            self.competing_errors: list[ApplicationConflictError] = []

        def execute(self, _metadata, validated_input, _request_context):
            self.calls += 1
            if self.calls == 1:
                assert self.service is not None
                try:
                    self.service.execute_revision_with_output(
                        ActorContext(actor_id="actor_1", user_id=None),
                        task_id="task_1",
                        task_input_revision_id="revision_1",
                        request_id="execute_request_loser",
                    )
                except ApplicationConflictError as error:
                    self.competing_errors.append(error)
            return replace(
                _success_output(),
                actual_runtime_parameters=dict(validated_input.runtime_parameters),
            )

        def readiness(self, _metadata):
            return "AVAILABLE"

    store = _Store(
        tasks={"task_1": _ready_task()},
        revisions={"revision_1": _revision()},
        llm_calls={"llm_1": _llm_call()},
        tool_runs={},
    )
    client = InterleavingClient()
    registry = ToolRegistry((build_zta35g_tool_definition(client),))
    ids = iter(("tool_run_winner", "tool_run_loser"))
    seeds = iter((101, 202))
    ticks = count()
    service = ToolExecutionService(
        lambda: _Uow(store),
        registry,
        clock=lambda: BASE + timedelta(seconds=20 + next(ticks)),
        id_factory=lambda: next(ids),
        seed_factory=lambda: next(seeds),
    )
    client.service = service

    winner = service.execute_revision_with_output(
        ActorContext(actor_id="actor_1", user_id=None),
        task_id="task_1",
        task_input_revision_id="revision_1",
        request_id="execute_request_winner",
    )

    assert set(store.tool_runs) == {winner.tool_run.tool_run_id}, {
        run_id: (run.attempt_no, run.current_status)
        for run_id, run in store.tool_runs.items()
    }
    assert [error.code for error in client.competing_errors] == [
        "RESOURCE_CONFLICT"
    ]
    assert winner.tool_run.tool_run_id == "tool_run_winner"
    assert winner.tool_run.attempt_no == 1
    assert store.tasks["task_1"].bound_tool_ref == _ready_task().bound_tool_ref
