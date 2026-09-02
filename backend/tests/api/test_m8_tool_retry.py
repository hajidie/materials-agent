from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Lock

import pytest
from sqlalchemy import func, select

from backend.tests.api.conftest import BASE_TIME
from backend.tests.api.test_assets import _MemoryStorage
from backend.tests.api.test_explanation_outcomes import (
    _CountingMemoryStorage,
    _Runtime,
    _chat_port_for_outputs,
)
from backend.tests.api.test_m8_explanation_retry import (
    _insert_later_failed_explanation,
)
from materialsagent.application.result_service import ResultService
from materialsagent.application.tool_execution import ToolExecutionService
from materialsagent.application.tool_registry import ToolRegistry
from materialsagent.application.tools import build_tool_registry
from materialsagent.application.zta35g_tool import build_zta35g_tool_definition
from materialsagent.domain.ports.tool_execution import (
    ToolClientUnavailableError,
)
from materialsagent.domain.ports.tool_registry import (
    ExecutionPolicy,
    ToolStatus,
)
from materialsagent.infrastructure.db.asset import AssetRow
from materialsagent.infrastructure.db.conversation_task import TaskRow
from materialsagent.infrastructure.db.explanation import (
    NaturalLanguageExplanationRow,
)
from materialsagent.infrastructure.db.idempotency_record import (
    IdempotencyRecordRow,
)
from materialsagent.infrastructure.db.tool_result import ToolResultRow
from materialsagent.infrastructure.db.tool_run import ToolRunRow
from materialsagent.infrastructure.db.session import create_session_factory
from materialsagent.infrastructure.db.unit_of_work import SQLAlchemyUnitOfWork
from materialsagent.domain.ports.unit_of_work import PersistenceError
from materialsagent.infrastructure.llm.mock_explanation import (
    MockExplanationAdapter,
)
from materialsagent.infrastructure.llm.mock import (
    MockChatOrchestrationAdapter,
    default_mock_responder,
)


class _Seeds:
    def __init__(self, *values: int) -> None:
        self._values = iter(values)
        self._lock = Lock()

    def __call__(self) -> int:
        with self._lock:
            return next(self._values)


class _UnavailableOnRetryRuntime(_Runtime):
    def __init__(self) -> None:
        super().__init__(partial=True)
        self.unavailable = False

    def execute(self, metadata, validated_input, context):
        if self.unavailable:
            self.calls += 1
            raise ToolClientUnavailableError()
        return super().execute(metadata, validated_input, context)


class _NthCommitUncertainUnitOfWork(SQLAlchemyUnitOfWork):
    def __init__(self, session_factory, controller) -> None:
        super().__init__(session_factory)
        self._controller = controller

    def commit(self) -> None:
        self._controller.commits += 1
        super().commit()
        if self._controller.commits == self._controller.fail_on_commit:
            raise PersistenceError("Commit result was uncertain.")


class _NthCommitUncertainFactory:
    def __init__(self, engine, *, fail_on_commit: int) -> None:
        self._session_factory = create_session_factory(engine)
        self.fail_on_commit = fail_on_commit
        self.commits = 0

    def __call__(self) -> SQLAlchemyUnitOfWork:
        return _NthCommitUncertainUnitOfWork(
            self._session_factory,
            self,
        )


class _RetrySelectionFaultToolResultRepository:
    def __init__(
        self,
        repository,
        *,
        mode: str,
        selected_result_id: str,
    ) -> None:
        self._repository = repository
        self._mode = mode
        self._selected_result_id = selected_result_id

    def get(self, result_id: str):
        result = self._repository.get(result_id)
        if result_id != self._selected_result_id:
            return result
        if self._mode == "SELECTED_RESULT_MISSING":
            return None
        if result is not None and self._mode == "RESULT_OTHER_RUN":
            object.__setattr__(
                result,
                "tool_run_id",
                "tool_run_corrupt_other",
            )
        return result

    def __getattr__(self, name: str):
        return getattr(self._repository, name)


class _RetrySelectionFaultToolRunRepository:
    def __init__(
        self,
        repository,
        *,
        mode: str,
        selected_tool_run_id: str,
    ) -> None:
        self._repository = repository
        self._mode = mode
        self._selected_tool_run_id = selected_tool_run_id

    def list_for_task(self, task_id: str):
        runs = self._repository.list_for_task(task_id)
        for run in runs:
            if run.tool_run_id != self._selected_tool_run_id:
                continue
            if self._mode == "RUN_RESULT_STATUS_MISMATCH":
                run.current_status = "PARTIALLY_SUCCEEDED"
            elif self._mode == "RUN_RESULT_OUTPUT_MISMATCH":
                run.failed_outputs = []
        return runs

    def __getattr__(self, name: str):
        return getattr(self._repository, name)


class _RetrySelectionFaultUnitOfWork:
    def __init__(
        self,
        unit_of_work,
        *,
        mode: str,
        selected_tool_run_id: str,
        selected_result_id: str,
    ) -> None:
        self._unit_of_work = unit_of_work
        self._mode = mode
        self._selected_tool_run_id = selected_tool_run_id
        self._selected_result_id = selected_result_id

    def __enter__(self):
        self._unit_of_work.__enter__()
        self.tool_runs = _RetrySelectionFaultToolRunRepository(
            self._unit_of_work.tool_runs,
            mode=self._mode,
            selected_tool_run_id=self._selected_tool_run_id,
        )
        self.tool_results = _RetrySelectionFaultToolResultRepository(
            self._unit_of_work.tool_results,
            mode=self._mode,
            selected_result_id=self._selected_result_id,
        )
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return self._unit_of_work.__exit__(
            exc_type,
            exc_value,
            traceback,
        )

    def __getattr__(self, name: str):
        return getattr(self._unit_of_work, name)


class _RetrySelectionFaultFactory:
    def __init__(
        self,
        factory,
        *,
        mode: str,
        selected_tool_run_id: str,
        selected_result_id: str,
    ) -> None:
        self._factory = factory
        self._mode = mode
        self._selected_tool_run_id = selected_tool_run_id
        self._selected_result_id = selected_result_id

    def __call__(self):
        return _RetrySelectionFaultUnitOfWork(
            self._factory(),
            mode=self._mode,
            selected_tool_run_id=self._selected_tool_run_id,
            selected_result_id=self._selected_result_id,
        )


def _client_options(
    api_harness,
    runtime,
    storage,
    explanation,
    seeds,
    *,
    unit_of_work_factory=None,
    tool_registry=None,
):
    factory = unit_of_work_factory or api_harness.unit_of_work_factory
    registry = tool_registry or build_tool_registry(runtime)
    options = {
        "tool_execution_service": ToolExecutionService(
            factory,
            registry,
            clock=lambda: BASE_TIME.replace(hour=1),
            seed_factory=seeds,
        ),
        "tool_registry": registry,
        "storage_service": storage,
        "explanation_port": explanation,
        "chat_orchestration_port": _chat_port_for_outputs(
            ["sem_image", "mechanical_properties"]
        ),
        "m7_tool_chain_enabled": True,
    }
    if unit_of_work_factory is not None:
        options["unit_of_work_factory"] = unit_of_work_factory
    return options


def _lifecycle_registry(runtime, *, version: str, policy: ExecutionPolicy):
    status = (
        ToolStatus.DEPRECATED
        if policy is ExecutionPolicy.EXISTING_TASK_ONLY
        else ToolStatus.DISABLED
    )
    return ToolRegistry(
        (
            replace(
                build_zta35g_tool_definition(runtime),
                version=version,
                status=status,
                execution_policy=policy,
            ),
        )
    )


def _create_failed_task(client) -> dict[str, object]:
    conversation = client.post(
        "/api/v1/conversations",
        json={},
    ).json()["data"]["conversation_id"]
    response = client.post(
        f"/api/v1/conversations/{conversation}/messages",
        headers={"Idempotency-Key": "initial-failed-task"},
        json={
            "submission_mode": "NEW_TASK",
            "content_text": "完整合法 Tool 请求",
        },
    )
    assert response.status_code == 200, response.json()
    assert response.json()["data"]["task"]["status"] in {
        "FAILED",
        "PARTIALLY_SUCCEEDED",
    }
    return response.json()["data"]


def test_retry_uses_current_compatible_version_and_policy_without_rewriting_history(
    api_harness,
) -> None:
    actor_id = "actor_m8_retry_version_refresh"
    api_harness.persist_actor(actor_id)
    runtime = _Runtime(partial=True)
    storage = _MemoryStorage()
    explanation = MockExplanationAdapter()
    with api_harness.create_client(
        actor_id,
        **_client_options(
            api_harness,
            runtime,
            storage,
            explanation,
            _Seeds(101),
        ),
    ) as client:
        initial = _create_failed_task(client)
    initial_run_id = initial["task"]["selected_tool_run_id"]
    task_id = initial["task"]["task_id"]
    with create_session_factory(api_harness.engine)() as session:
        old_before = session.get(ToolRunRow, initial_run_id)
        assert old_before is not None
        old_facts = (
            old_before.current_status,
            old_before.tool_version,
            old_before.schema_hash,
            old_before.execution_policy_snapshot,
            old_before.error_code,
        )

    runtime.partial = False
    current_registry = _lifecycle_registry(
        runtime,
        version="2",
        policy=ExecutionPolicy.EXISTING_TASK_ONLY,
    )
    with api_harness.create_client(
        actor_id,
        **_client_options(
            api_harness,
            runtime,
            storage,
            explanation,
            _Seeds(202),
            tool_registry=current_registry,
        ),
    ) as client:
        retried = client.post(
            f"/api/v1/tasks/{task_id}/tool-runs",
            headers={"Idempotency-Key": "version-refresh-retry"},
            json={},
        )

    assert retried.status_code == 200, retried.json()
    payload = retried.json()["data"]
    with create_session_factory(api_harness.engine)() as session:
        old_after = session.get(ToolRunRow, initial_run_id)
        new_run = session.get(
            ToolRunRow,
            payload["tool_run"]["tool_run_id"],
        )
        task_after = session.get(TaskRow, task_id)
        assert old_after is not None
        assert new_run is not None
        assert task_after is not None
        assert new_run.tool_version == "2"
        assert (
            new_run.execution_policy_snapshot
            == ExecutionPolicy.EXISTING_TASK_ONLY
        )
        assert task_after.bound_tool_version == "1"
        assert (
            old_after.current_status,
            old_after.tool_version,
            old_after.schema_hash,
            old_after.execution_policy_snapshot,
            old_after.error_code,
        ) == old_facts


def test_disabled_current_policy_rejects_retry_without_new_attempt(
    api_harness,
) -> None:
    actor_id = "actor_m8_retry_disabled_policy"
    api_harness.persist_actor(actor_id)
    runtime = _Runtime(partial=True)
    storage = _MemoryStorage()
    explanation = MockExplanationAdapter()
    with api_harness.create_client(
        actor_id,
        **_client_options(
            api_harness,
            runtime,
            storage,
            explanation,
            _Seeds(101),
        ),
    ) as client:
        initial = _create_failed_task(client)
    task_id = initial["task"]["task_id"]
    with create_session_factory(api_harness.engine)() as session:
        run_count_before = session.scalar(
            select(func.count()).select_from(ToolRunRow)
        )

    disabled_registry = _lifecycle_registry(
        runtime,
        version="2",
        policy=ExecutionPolicy.NONE,
    )
    with api_harness.create_client(
        actor_id,
        **_client_options(
            api_harness,
            runtime,
            storage,
            explanation,
            _Seeds(),
            tool_registry=disabled_registry,
        ),
    ) as client:
        denied = client.post(
            f"/api/v1/tasks/{task_id}/tool-runs",
            headers={"Idempotency-Key": "disabled-policy-retry"},
            json={},
        )

    assert denied.status_code == 409, denied.json()
    assert denied.json()["error"]["code"] == "TOOL_EXECUTION_NOT_ALLOWED"
    with create_session_factory(api_harness.engine)() as session:
        assert session.scalar(
            select(func.count()).select_from(ToolRunRow)
        ) == run_count_before


def test_tool_retry_success_replay_conflict_and_immutable_history(
    api_harness,
) -> None:
    actor_id = "actor_m8_tool_retry"
    api_harness.persist_actor(actor_id)
    runtime = _Runtime(partial=True)
    storage = _MemoryStorage()
    explanation = MockExplanationAdapter()
    with api_harness.create_client(
        actor_id,
        **_client_options(
            api_harness,
            runtime,
            storage,
            explanation,
            _Seeds(101, 202),
        ),
    ) as client:
        initial = _create_failed_task(client)
        initial_run_id = initial["task"]["selected_tool_run_id"]
        initial_result_id = initial["task"]["selected_result_id"]
        task_id = initial["task"]["task_id"]
        runtime.partial = False
        first = client.post(
            f"/api/v1/tasks/{task_id}/tool-runs",
            headers={"Idempotency-Key": "retry-key"},
            json={},
        )
        replay = client.post(
            f"/api/v1/tasks/{task_id}/tool-runs",
            headers={"Idempotency-Key": "retry-key"},
            json={},
        )
        conflict = client.post(
            f"/api/v1/tasks/{task_id}/tool-runs",
            headers={"Idempotency-Key": "retry-key"},
            json={"reason": "different"},
        )
        ineligible = client.post(
            f"/api/v1/tasks/{task_id}/tool-runs",
            headers={"Idempotency-Key": "retry-after-success"},
            json={},
        )
        missing_key = client.post(
            f"/api/v1/tasks/{task_id}/tool-runs",
            json={},
        )

    assert first.status_code == replay.status_code == 200, (
        first.json(),
        replay.json(),
    )
    assert first.json()["data"]["idempotency_replayed"] is False
    assert replay.json()["data"]["idempotency_replayed"] is True
    assert first.json()["data"]["tool_run"]["attempt_no"] == 2
    assert (
        first.json()["data"]["tool_run"]["tool_run_id"]
        == replay.json()["data"]["tool_run"]["tool_run_id"]
    )
    assert first.json()["data"]["task_status"] == "SUCCEEDED"
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"
    assert ineligible.status_code == 409
    assert ineligible.json()["error"]["code"] == "TASK_NOT_RETRYABLE"
    assert missing_key.status_code == 422
    assert runtime.calls == 2
    assert explanation.call_count == 2

    with create_session_factory(api_harness.engine)() as session:
        runs = list(
            session.scalars(
                select(ToolRunRow).order_by(ToolRunRow.attempt_no)
            ).all()
        )
        results = list(
            session.scalars(
                select(ToolResultRow).order_by(ToolResultRow.created_at)
            ).all()
        )
        assert len(runs) == len(results) == 2
        assert runs[0].tool_run_id == initial_run_id
        assert results[0].result_id == initial_result_id
        assert runs[0].execution_input["runtime_parameters"]["seed"] == 101
        assert runs[1].execution_input["runtime_parameters"]["seed"] == 202
        assert session.scalar(select(func.count()).select_from(AssetRow)) == 2
        assert session.scalar(
            select(func.count()).select_from(NaturalLanguageExplanationRow)
        ) == 2
        assert session.scalar(
            select(func.count())
            .select_from(IdempotencyRecordRow)
            .where(IdempotencyRecordRow.operation == "TOOL_RETRY")
        ) == 1


def test_twenty_concurrent_tool_retries_reserve_one_attempt_and_call_runtime_once(
    api_harness,
) -> None:
    actor_id = "actor_m8_tool_retry_concurrent"
    api_harness.persist_actor(actor_id)
    runtime = _Runtime(partial=True)
    explanation = MockExplanationAdapter()
    storage = _CountingMemoryStorage()
    with api_harness.create_client(
        actor_id,
        **_client_options(
            api_harness,
            runtime,
            storage,
            explanation,
            _Seeds(301, 302),
        ),
    ) as client:
        initial = _create_failed_task(client)
        task_id = initial["task"]["task_id"]
        puts_before_retry = storage.put_calls
        runtime.partial = False

        def retry(_: int):
            return client.post(
                f"/api/v1/tasks/{task_id}/tool-runs",
                headers={"Idempotency-Key": "same-retry"},
                json={},
            )

        with ThreadPoolExecutor(max_workers=20) as executor:
            responses = list(executor.map(retry, range(20)))

    assert {response.status_code for response in responses} == {200}, [
        response.json()
        for response in responses
        if response.status_code != 200
    ]
    payloads = [response.json()["data"] for response in responses]
    assert len(
        {payload["tool_run"]["tool_run_id"] for payload in payloads}
    ) == 1
    assert sum(not payload["idempotency_replayed"] for payload in payloads) == 1
    assert runtime.calls == 2
    assert storage.put_calls == puts_before_retry + 1
    assert explanation.call_count == 2
    with api_harness.engine.connect() as connection:
        assert connection.scalar(
            select(func.count()).select_from(ToolRunRow)
        ) == 2
        assert connection.scalar(
            select(func.count())
            .select_from(IdempotencyRecordRow)
            .where(IdempotencyRecordRow.operation == "TOOL_RETRY")
        ) == 1


def test_failed_retry_is_stable_and_same_key_replay_never_calls_runtime_again(
    api_harness,
) -> None:
    actor_id = "actor_m8_tool_retry_failure"
    api_harness.persist_actor(actor_id)
    runtime = _UnavailableOnRetryRuntime()
    storage = _CountingMemoryStorage()
    explanation = MockExplanationAdapter()
    with api_harness.create_client(
        actor_id,
        **_client_options(
            api_harness,
            runtime,
            storage,
            explanation,
            _Seeds(401, 402),
        ),
    ) as client:
        initial = _create_failed_task(client)
        task_id = initial["task"]["task_id"]
        runtime.unavailable = True
        failed = client.post(
            f"/api/v1/tasks/{task_id}/tool-runs",
            headers={"Idempotency-Key": "failed-retry"},
            json={},
        )
        replay = client.post(
            f"/api/v1/tasks/{task_id}/tool-runs",
            headers={"Idempotency-Key": "failed-retry"},
            json={},
        )

    assert failed.status_code == 503
    assert failed.json()["error"]["code"] == "RUNTIME_UNAVAILABLE"
    assert replay.status_code == 200, replay.json()
    data = replay.json()["data"]
    assert data["idempotency_replayed"] is True
    assert data["task_status"] == "FAILED"
    assert data["tool_run"]["status"] == "FAILED"
    assert data["tool_run"]["attempt_no"] == 2
    assert data["result_id"] is None
    assert runtime.calls == 2
    assert storage.put_calls == 1
    assert explanation.call_count == 1


def test_tool_retry_own_reservation_commit_uncertainty_continues_once(
    api_harness,
) -> None:
    actor_id = "actor_m8_tool_retry_reservation_uncertainty"
    api_harness.persist_actor(actor_id)
    runtime = _Runtime(partial=True)
    storage = _MemoryStorage()
    explanation = MockExplanationAdapter()
    with api_harness.create_client(
        actor_id,
        **_client_options(
            api_harness,
            runtime,
            storage,
            explanation,
            _Seeds(451),
        ),
    ) as client:
        initial = _create_failed_task(client)
    task_id = initial["task"]["task_id"]
    runtime.partial = False
    factory = _NthCommitUncertainFactory(
        api_harness.engine,
        fail_on_commit=1,
    )
    with api_harness.create_client(
        actor_id,
        **_client_options(
            api_harness,
            runtime,
            storage,
            explanation,
            _Seeds(452),
            unit_of_work_factory=factory,
        ),
    ) as client:
        first = client.post(
            f"/api/v1/tasks/{task_id}/tool-runs",
            headers={"Idempotency-Key": "uncertain-tool-reservation"},
            json={},
        )
        replay = client.post(
            f"/api/v1/tasks/{task_id}/tool-runs",
            headers={"Idempotency-Key": "uncertain-tool-reservation"},
            json={},
        )

    assert first.status_code == replay.status_code == 200, (
        first.json(),
        replay.json(),
    )
    assert first.json()["data"]["idempotency_replayed"] is False
    assert replay.json()["data"]["idempotency_replayed"] is True
    assert first.json()["data"]["tool_run"]["status"] == "SUCCEEDED"
    assert first.json()["data"]["task_status"] == "SUCCEEDED"
    assert runtime.calls == 2
    assert explanation.call_count == 2


def test_tool_retry_start_commit_uncertainty_calls_runtime_once(
    api_harness,
) -> None:
    actor_id = "actor_m8_tool_retry_start_uncertainty"
    api_harness.persist_actor(actor_id)
    runtime = _Runtime(partial=True)
    storage = _MemoryStorage()
    explanation = MockExplanationAdapter()
    with api_harness.create_client(
        actor_id,
        **_client_options(
            api_harness,
            runtime,
            storage,
            explanation,
            _Seeds(461),
        ),
    ) as client:
        initial = _create_failed_task(client)
    task_id = initial["task"]["task_id"]
    runtime.partial = False
    factory = _NthCommitUncertainFactory(
        api_harness.engine,
        fail_on_commit=2,
    )
    with api_harness.create_client(
        actor_id,
        **_client_options(
            api_harness,
            runtime,
            storage,
            explanation,
            _Seeds(462),
            unit_of_work_factory=factory,
        ),
    ) as client:
        response = client.post(
            f"/api/v1/tasks/{task_id}/tool-runs",
            headers={"Idempotency-Key": "uncertain-tool-start"},
            json={},
        )

    assert response.status_code == 200, response.json()
    assert response.json()["data"]["idempotency_replayed"] is False
    assert response.json()["data"]["tool_run"]["status"] == "SUCCEEDED"
    assert response.json()["data"]["task_status"] == "SUCCEEDED"
    assert runtime.calls == 2
    assert explanation.call_count == 2


def test_tool_retry_runtime_success_commit_uncertainty_completes_once(
    api_harness,
) -> None:
    actor_id = "actor_m8_tool_retry_runtime_success_uncertainty"
    api_harness.persist_actor(actor_id)
    runtime = _Runtime(partial=True)
    storage = _CountingMemoryStorage()
    explanation = MockExplanationAdapter()
    with api_harness.create_client(
        actor_id,
        **_client_options(
            api_harness,
            runtime,
            storage,
            explanation,
            _Seeds(463),
        ),
    ) as client:
        initial = _create_failed_task(client)
    task_id = initial["task"]["task_id"]
    runtime.partial = False
    factory = _NthCommitUncertainFactory(
        api_harness.engine,
        fail_on_commit=3,
    )
    with api_harness.create_client(
        actor_id,
        **_client_options(
            api_harness,
            runtime,
            storage,
            explanation,
            _Seeds(464),
            unit_of_work_factory=factory,
        ),
    ) as client:
        response = client.post(
            f"/api/v1/tasks/{task_id}/tool-runs",
            headers={"Idempotency-Key": "uncertain-tool-runtime-success"},
            json={},
        )

    assert response.status_code == 200, response.json()
    assert response.json()["data"]["tool_run"]["status"] == "SUCCEEDED"
    assert response.json()["data"]["task_status"] == "SUCCEEDED"
    assert runtime.calls == 2
    assert storage.put_calls == 2
    assert explanation.call_count == 2
    with api_harness.engine.connect() as connection:
        assert connection.scalar(
            select(func.count()).select_from(ToolRunRow)
        ) == 2
        assert connection.scalar(
            select(func.count()).select_from(ToolResultRow)
        ) == 2


def test_tool_retry_result_commit_uncertainty_continues_explanation_once(
    api_harness,
) -> None:
    actor_id = "actor_m8_tool_retry_result_uncertainty"
    api_harness.persist_actor(actor_id)
    runtime = _Runtime(partial=True)
    storage = _CountingMemoryStorage()
    explanation = MockExplanationAdapter()
    with api_harness.create_client(
        actor_id,
        **_client_options(
            api_harness,
            runtime,
            storage,
            explanation,
            _Seeds(465),
        ),
    ) as client:
        initial = _create_failed_task(client)
    task_id = initial["task"]["task_id"]
    runtime.partial = False
    result_factory = _NthCommitUncertainFactory(
        api_harness.engine,
        fail_on_commit=1,
    )
    options = _client_options(
        api_harness,
        runtime,
        storage,
        explanation,
        _Seeds(466),
    )
    options["result_service"] = ResultService(
        result_factory,
        clock=lambda: BASE_TIME.replace(hour=1),
    )
    with api_harness.create_client(actor_id, **options) as client:
        response = client.post(
            f"/api/v1/tasks/{task_id}/tool-runs",
            headers={"Idempotency-Key": "uncertain-tool-result"},
            json={},
        )

    assert response.status_code == 200, response.json()
    assert response.json()["data"]["tool_run"]["status"] == "SUCCEEDED"
    assert response.json()["data"]["task_status"] == "SUCCEEDED"
    assert response.json()["data"]["explanation_status"] == "SUCCEEDED"
    assert runtime.calls == 2
    assert storage.put_calls == 2
    assert explanation.call_count == 2
    with api_harness.engine.connect() as connection:
        assert connection.scalar(
            select(func.count()).select_from(ToolResultRow)
        ) == 2


def test_tool_retry_projection_keeps_latest_success_when_later_failure_exists(
    api_harness,
) -> None:
    actor_id = "actor_m8_tool_retry_explanation_selection"
    api_harness.persist_actor(actor_id)
    runtime = _Runtime(partial=True)
    explanation = MockExplanationAdapter()
    with api_harness.create_client(
        actor_id,
        **_client_options(
            api_harness,
            runtime,
            _MemoryStorage(),
            explanation,
            _Seeds(471, 472),
        ),
    ) as client:
        initial = _create_failed_task(client)
        task_id = initial["task"]["task_id"]
        with create_session_factory(api_harness.engine)() as session:
            task_row = session.get(TaskRow, task_id)
            assert task_row is not None
            conversation_id = task_row.conversation_id
        runtime.partial = False
        first = client.post(
            f"/api/v1/tasks/{task_id}/tool-runs",
            headers={"Idempotency-Key": "tool-selection-retry"},
            json={},
        )
        assert first.status_code == 200, first.json()
        first_data = first.json()["data"]
        _insert_later_failed_explanation(
            api_harness,
            conversation_id=conversation_id,
            task_id=task_id,
            result_id=first_data["result_id"],
        )
        replay = client.post(
            f"/api/v1/tasks/{task_id}/tool-runs",
            headers={"Idempotency-Key": "tool-selection-retry"},
            json={},
        )

    assert replay.status_code == 200, replay.json()
    assert replay.json()["data"]["idempotency_replayed"] is True
    assert replay.json()["data"]["explanation_id"] == first_data["explanation_id"]
    assert replay.json()["data"]["explanation_status"] == "SUCCEEDED"


def test_different_concurrent_retry_keys_still_create_only_one_attempt(
    api_harness,
) -> None:
    actor_id = "actor_m8_tool_retry_distinct_keys"
    api_harness.persist_actor(actor_id)
    runtime = _Runtime(partial=True)
    explanation = MockExplanationAdapter()
    with api_harness.create_client(
        actor_id,
        **_client_options(
            api_harness,
            runtime,
            _MemoryStorage(),
            explanation,
            _Seeds(501, 502),
        ),
    ) as client:
        initial = _create_failed_task(client)
        task_id = initial["task"]["task_id"]
        runtime.partial = False

        def retry(index: int):
            return client.post(
                f"/api/v1/tasks/{task_id}/tool-runs",
                headers={"Idempotency-Key": f"distinct-retry-{index}"},
                json={},
            )

        with ThreadPoolExecutor(max_workers=20) as executor:
            responses = list(executor.map(retry, range(20)))

    assert sum(response.status_code == 200 for response in responses) == 1
    rejected = [
        response
        for response in responses
        if response.status_code != 200
    ]
    assert {response.status_code for response in rejected} == {409}
    assert {
        response.json()["error"]["code"]
        for response in rejected
    } == {"TASK_NOT_RETRYABLE"}
    assert runtime.calls == 2
    with api_harness.engine.connect() as connection:
        assert connection.scalar(
            select(func.count()).select_from(ToolRunRow)
        ) == 2
        assert connection.scalar(
            select(func.count())
            .select_from(IdempotencyRecordRow)
            .where(IdempotencyRecordRow.operation == "TOOL_RETRY")
        ) == 1


def test_retry_endpoints_hide_foreign_task_and_result(
    api_harness,
) -> None:
    owner_id = "actor_m8_retry_owner"
    other_id = "actor_m8_retry_other"
    api_harness.persist_actor(owner_id)
    api_harness.persist_actor(other_id)
    runtime = _Runtime(partial=True)
    explanation = MockExplanationAdapter()
    options = _client_options(
        api_harness,
        runtime,
        _MemoryStorage(),
        explanation,
        _Seeds(601, 602),
    )
    with api_harness.create_client(owner_id, **options) as owner:
        initial = _create_failed_task(owner)
    task_id = initial["task"]["task_id"]
    result_id = initial["task"]["selected_result_id"]
    with api_harness.create_client(other_id, **options) as other:
        tool_response = other.post(
            f"/api/v1/tasks/{task_id}/tool-runs",
            headers={"Idempotency-Key": "foreign-tool-retry"},
            json={},
        )
        explanation_response = other.post(
            f"/api/v1/tool-results/{result_id}/explanations",
            headers={"Idempotency-Key": "foreign-explanation-retry"},
            json={},
        )

    assert tool_response.status_code == explanation_response.status_code == 404
    assert (
        tool_response.json()["error"]["code"]
        == explanation_response.json()["error"]["code"]
        == "RESOURCE_NOT_FOUND"
    )
    assert runtime.calls == 1


def test_tool_retry_rejects_knowledge_needs_input_and_hard_validation_failure(
    api_harness,
) -> None:
    actor_id = "actor_m8_retry_ineligible_states"
    api_harness.persist_actor(actor_id)
    runtime = _Runtime()
    options = _client_options(
        api_harness,
        runtime,
        _MemoryStorage(),
        MockExplanationAdapter(),
        _Seeds(701),
    )
    options["chat_orchestration_port"] = MockChatOrchestrationAdapter(
        default_mock_responder
    )
    cases = [
        ("什么是 ZTA35G？", "knowledge"),
        ("缺 aging_temperature", "needs-input"),
        ("越界温度", "hard-validation"),
    ]
    with api_harness.create_client(actor_id, **options) as client:
        for content_text, suffix in cases:
            conversation_id = client.post(
                "/api/v1/conversations",
                json={},
            ).json()["data"]["conversation_id"]
            submitted = client.post(
                f"/api/v1/conversations/{conversation_id}/messages",
                headers={"Idempotency-Key": f"source-{suffix}"},
                json={
                    "submission_mode": "NEW_TASK",
                    "content_text": content_text,
                },
            )
            task_id = (
                submitted.json()["resource"]["task_id"]
                if suffix == "hard-validation"
                else submitted.json()["data"]["task"]["task_id"]
            )
            retry = client.post(
                f"/api/v1/tasks/{task_id}/tool-runs",
                headers={"Idempotency-Key": f"retry-{suffix}"},
                json={},
            )
            assert retry.status_code == 409, retry.json()
            assert retry.json()["error"]["code"] == "TASK_NOT_RETRYABLE"

    assert runtime.calls == 0


def test_tool_retry_rejects_both_null_selection_with_prior_history(
    api_harness,
) -> None:
    actor_id = "actor_m8_retry_both_null_with_history"
    api_harness.persist_actor(actor_id)
    runtime = _Runtime(failed=True)
    storage = _CountingMemoryStorage()
    explanation = MockExplanationAdapter()
    options = _client_options(
        api_harness,
        runtime,
        storage,
        explanation,
        _Seeds(791, 792),
    )
    options["chat_orchestration_port"] = _chat_port_for_outputs(
        ["mechanical_properties"]
    )
    with api_harness.create_client(actor_id, **options) as client:
        initial = _create_failed_task(client)
    task_id = initial["task"]["task_id"]
    old_run_id = initial["task"]["selected_tool_run_id"]
    old_result_id = initial["task"]["selected_result_id"]
    assert old_run_id is not None
    assert old_result_id is not None

    session_factory = create_session_factory(api_harness.engine)
    with session_factory() as session:
        task_row = session.get(TaskRow, task_id)
        assert task_row is not None
        task_row.selected_tool_run_id = None
        task_row.selected_result_id = None
        session.commit()

    runtime.failed = False
    with api_harness.create_client(
        actor_id,
        **_client_options(
            api_harness,
            runtime,
            storage,
            explanation,
            _Seeds(792),
        ),
    ) as client:
        response = client.post(
            f"/api/v1/tasks/{task_id}/tool-runs",
            headers={"Idempotency-Key": "both-null-history-retry"},
            json={},
        )

    assert response.status_code == 409, response.json()
    assert response.json()["error"]["code"] == "TASK_NOT_RETRYABLE"
    assert runtime.calls == 1
    assert storage.put_calls == 1
    assert explanation.call_count == 1
    with api_harness.engine.connect() as connection:
        assert connection.scalar(
            select(func.count()).select_from(ToolRunRow)
        ) == 1
        assert connection.scalar(
            select(func.count()).select_from(ToolResultRow)
        ) == 1
        assert connection.scalar(
            select(func.count())
            .select_from(IdempotencyRecordRow)
            .where(IdempotencyRecordRow.operation == "TOOL_RETRY")
        ) == 0
        old_run = connection.execute(
            select(
                ToolRunRow.current_status,
            ).where(
                ToolRunRow.tool_run_id == old_run_id
            )
        ).one()
        old_result = connection.execute(
            select(
                ToolResultRow.status,
            ).where(
                ToolResultRow.result_id == old_result_id
            )
        ).one()
        assert old_run.current_status == "FAILED"
        assert old_result.status == "FAILED"


@pytest.mark.parametrize(
    "mode",
    [
        "SELECTED_RESULT_MISSING",
        "RESULT_OTHER_RUN",
        "RUN_RESULT_STATUS_MISMATCH",
        "RUN_RESULT_OUTPUT_MISMATCH",
    ],
)
def test_tool_retry_rejects_corrupt_selected_facts_before_reservation(
    api_harness,
    mode: str,
) -> None:
    actor_id = f"actor_m8_retry_reservation_{mode.lower()}"
    api_harness.persist_actor(actor_id)
    runtime = _Runtime(failed=True)
    storage = _CountingMemoryStorage()
    explanation = MockExplanationAdapter()
    initial_options = _client_options(
        api_harness,
        runtime,
        storage,
        explanation,
        _Seeds(821, 822),
    )
    initial_options["chat_orchestration_port"] = _chat_port_for_outputs(
        ["mechanical_properties"]
    )
    with api_harness.create_client(
        actor_id,
        **initial_options,
    ) as client:
        initial = _create_failed_task(client)

    task_id = initial["task"]["task_id"]
    selected_tool_run_id = initial["task"]["selected_tool_run_id"]
    selected_result_id = initial["task"]["selected_result_id"]
    assert selected_tool_run_id is not None
    assert selected_result_id is not None

    with api_harness.engine.connect() as connection:
        task_before = connection.execute(
            select(
                TaskRow.current_status,
                TaskRow.selected_tool_run_id,
                TaskRow.selected_result_id,
            ).where(TaskRow.task_id == task_id)
        ).one()
        tool_run_count_before = connection.scalar(
            select(func.count()).select_from(ToolRunRow)
        )
        retry_record_count_before = connection.scalar(
            select(func.count())
            .select_from(IdempotencyRecordRow)
            .where(IdempotencyRecordRow.operation == "TOOL_RETRY")
        )
    calls_before = (
        runtime.calls,
        storage.put_calls,
        explanation.call_count,
    )

    runtime.failed = False
    fault_factory = _RetrySelectionFaultFactory(
        api_harness.unit_of_work_factory,
        mode=mode,
        selected_tool_run_id=selected_tool_run_id,
        selected_result_id=selected_result_id,
    )
    retry_options = _client_options(
        api_harness,
        runtime,
        storage,
        explanation,
        _Seeds(822),
        unit_of_work_factory=fault_factory,
    )
    retry_options["chat_orchestration_port"] = _chat_port_for_outputs(
        ["mechanical_properties"]
    )
    with api_harness.create_client(
        actor_id,
        **retry_options,
    ) as client:
        response = client.post(
            f"/api/v1/tasks/{task_id}/tool-runs",
            headers={
                "Idempotency-Key": f"corrupt-selection-{mode.lower()}"
            },
            json={},
        )

    assert response.status_code == 409, response.json()
    assert response.json()["error"]["code"] == "TASK_NOT_RETRYABLE"
    assert (
        runtime.calls,
        storage.put_calls,
        explanation.call_count,
    ) == calls_before
    with api_harness.engine.connect() as connection:
        task_after = connection.execute(
            select(
                TaskRow.current_status,
                TaskRow.selected_tool_run_id,
                TaskRow.selected_result_id,
            ).where(TaskRow.task_id == task_id)
        ).one()
        assert task_after == task_before
        assert connection.scalar(
            select(func.count()).select_from(ToolRunRow)
        ) == tool_run_count_before
        assert connection.scalar(
            select(func.count())
            .select_from(IdempotencyRecordRow)
            .where(IdempotencyRecordRow.operation == "TOOL_RETRY")
        ) == retry_record_count_before


def test_valid_tool_unavailable_task_without_prior_run_can_retry(
    api_harness,
) -> None:
    actor_id = "actor_m8_retry_without_prior_run"
    api_harness.persist_actor(actor_id)
    with api_harness.create_client(actor_id) as initial_client:
        conversation_id = initial_client.post(
            "/api/v1/conversations",
            json={},
        ).json()["data"]["conversation_id"]
        initial = initial_client.post(
            f"/api/v1/conversations/{conversation_id}/messages",
            headers={"Idempotency-Key": "tool-unavailable-source"},
            json={
                "submission_mode": "NEW_TASK",
                "content_text": "完整合法 Tool 请求",
            },
        )
    assert initial.status_code == 503
    assert initial.json()["error"]["code"] == "TOOL_UNAVAILABLE"
    task_id = initial.json()["resource"]["task_id"]

    runtime = _Runtime()
    with api_harness.create_client(
        actor_id,
        **_client_options(
            api_harness,
            runtime,
            _MemoryStorage(),
            MockExplanationAdapter(),
            _Seeds(801),
        ),
    ) as retry_client:
        retried = retry_client.post(
            f"/api/v1/tasks/{task_id}/tool-runs",
            headers={"Idempotency-Key": "tool-unavailable-retry"},
            json={},
        )

    assert retried.status_code == 200, retried.json()
    assert retried.json()["data"]["task_status"] == "SUCCEEDED"
    assert retried.json()["data"]["tool_run"]["attempt_no"] == 1
    assert runtime.calls == 1
