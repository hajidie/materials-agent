from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select

from materialsagent.application.tool_execution import ToolExecutionService
from materialsagent.application.tools import build_tool_registry
from materialsagent.domain.ports.tool_execution import (
    ToolClientTimeoutError,
    ToolExecutionOutput,
)
from materialsagent.infrastructure.db.conversation_task import (
    TaskInputRevisionRow,
)
from materialsagent.infrastructure.db.session import create_session_factory
from materialsagent.infrastructure.db.tool_run import ToolRunRow
from materialsagent.infrastructure.db.unit_of_work import SQLAlchemyUnitOfWork
from materialsagent.domain.ports.unit_of_work import PersistenceError


class _ToolClient:
    def __init__(self, outcome: object | None = None) -> None:
        self.outcome = outcome or ToolExecutionOutput(
            status="SUCCEEDED",
            requested_outputs=("sem_image", "mechanical_properties"),
            completed_outputs=("sem_image", "mechanical_properties"),
            failed_outputs=(),
            data={"mechanical_properties": {"yield_strength_mpa": 1000.0}},
            images=(),
            warnings=(),
            diagnostics=(
                {"step": "mock_inference", "status": "SUCCEEDED"},
            ),
            actual_runtime_parameters={
                "seed": 0,
                "num_samples": 1,
                "guide_scale": 2.0,
                "timesteps": 1000,
            },
            model_bundle_id="mock-zta35g-v1",
            error=None,
        )
        self.calls = 0

    def execute(self, _metadata, validated_input, _context):
        self.calls += 1
        if isinstance(self.outcome, Exception):
            raise self.outcome
        runtime_parameters = dict(validated_input.runtime_parameters)
        return ToolExecutionOutput(
            status=self.outcome.status,
            requested_outputs=tuple(validated_input.requested_outputs),
            completed_outputs=tuple(validated_input.requested_outputs),
            failed_outputs=(),
            data=dict(self.outcome.data),
            images=tuple(self.outcome.images),
            warnings=tuple(self.outcome.warnings),
            diagnostics=tuple(self.outcome.diagnostics),
            actual_runtime_parameters=runtime_parameters,
            model_bundle_id=self.outcome.model_bundle_id,
            error=None,
        )

    def readiness(self, _metadata) -> str:
        return "AVAILABLE"


class _FailingThirdCommitUnitOfWork(SQLAlchemyUnitOfWork):
    def __init__(self, session_factory, commit_counter: list[int]) -> None:
        super().__init__(session_factory)
        self._commit_counter = commit_counter

    def commit(self) -> None:
        self._commit_counter[0] += 1
        if self._commit_counter[0] == 3:
            raise PersistenceError("Persistence operation failed.")
        super().commit()


def _settings(api_harness, *, enabled: bool):
    return api_harness.settings.model_copy(
        update={"m5_dev_routes_enabled": enabled}
    )


def _create_valid_revision(client, api_harness) -> tuple[str, str]:
    conversation = client.post("/api/v1/conversations", json={})
    assert conversation.status_code == 201
    conversation_id = conversation.json()["data"]["conversation_id"]
    message = client.post(
        f"/api/v1/conversations/{conversation_id}/messages",
        json={
            "submission_mode": "NEW_TASK",
            "content_text": "完整合法 Tool 请求",
        },
    )
    assert message.status_code == 503
    task_id = message.json()["resource"]["task_id"]
    with create_session_factory(api_harness.engine)() as session:
        revision = session.scalar(
            select(TaskInputRevisionRow).where(
                TaskInputRevisionRow.task_id == task_id
            )
        )
        assert revision is not None
        return task_id, revision.task_input_revision_id


def test_catalog_list_detail_and_unknown_tool_are_safely_projected(
    api_harness,
) -> None:
    actor_id = "actor_catalog"
    api_harness.persist_actor(actor_id)
    registry = build_tool_registry(_ToolClient())

    with api_harness.create_client(actor_id, tool_registry=registry) as client:
        listing = client.get("/api/v1/tools")
        detail = client.get("/api/v1/tools/zta35g_sem_virtual_lab")
        unknown = client.get("/api/v1/tools/not-registered")

    assert listing.status_code == 200
    assert len(listing.json()["data"]) == 1
    entry = listing.json()["data"][0]
    assert set(entry) == {
        "tool_id",
        "display_name",
        "description",
        "material_scope",
        "enabled",
        "availability",
        "tool_version",
        "schema_version",
        "supported_outputs",
        "input_fields",
        "output_summary",
        "supported_asset_types",
        "limitations",
    }
    assert entry["tool_id"] == "zta35g_sem_virtual_lab"
    assert entry["tool_version"] == "0.1.0"
    assert entry["schema_version"] == "1.0"
    assert entry["availability"] == "AVAILABLE"
    assert detail.status_code == 200
    assert detail.json()["data"] == entry
    assert unknown.status_code == 404
    assert unknown.json()["error"]["code"] == "RESOURCE_NOT_FOUND"


def test_dev_execution_path_is_hidden_when_disabled(api_harness) -> None:
    actor_id = "actor_disabled"
    api_harness.persist_actor(actor_id)

    with api_harness.create_client(
        actor_id,
        settings=_settings(api_harness, enabled=False),
    ) as client:
        response = client.post(
            "/api/v1/dev/tasks/task_hidden/tool-runs",
            json={"task_input_revision_id": "revision_hidden"},
        )

    assert response.status_code == 404


def test_enabled_dev_path_executes_revision_and_queries_safe_running_tool_run(
    api_harness,
) -> None:
    actor_id = "actor_execute"
    api_harness.persist_actor(actor_id)
    tool_client = _ToolClient()
    registry = build_tool_registry(tool_client)

    with api_harness.create_client(
        actor_id,
        settings=_settings(api_harness, enabled=True),
        tool_registry=registry,
    ) as client:
        task_id, revision_id = _create_valid_revision(client, api_harness)
        execute = client.post(
            f"/api/v1/dev/tasks/{task_id}/tool-runs",
            json={"task_input_revision_id": revision_id},
        )
        assert execute.status_code == 201
        run_data = execute.json()["data"]
        query = client.get(
            f"/api/v1/dev/tool-runs/{run_data['tool_run_id']}"
        )
        task = client.get(f"/api/v1/tasks/{task_id}")

    assert tool_client.calls == 1
    assert run_data["status"] == "RUNNING"
    assert run_data["completed_outputs"] == []
    assert run_data["failed_outputs"] == []
    assert run_data["output_summary"]["runtime_status"] == "SUCCEEDED"
    assert "data_base64" not in repr(run_data)
    assert query.status_code == 200
    assert query.json()["data"] == run_data
    assert task.json()["data"]["status"] == "FAILED"
    assert task.json()["data"]["error_code"] == "TOOL_UNAVAILABLE"
    assert task.json()["data"]["selected_tool_run_id"] is None
    assert task.json()["data"]["selected_result_id"] is None
    with create_session_factory(api_harness.engine)() as session:
        stored = session.scalar(
            select(ToolRunRow).where(ToolRunRow.tool_run_id == run_data["tool_run_id"])
        )
        assert stored is not None
        assert stored.current_status == "RUNNING"


def test_runtime_failure_returns_persisted_safe_tool_run_resource(
    api_harness,
) -> None:
    actor_id = "actor_timeout"
    api_harness.persist_actor(actor_id)
    tool_client = _ToolClient(ToolClientTimeoutError())

    with api_harness.create_client(
        actor_id,
        settings=_settings(api_harness, enabled=True),
        tool_registry=build_tool_registry(tool_client),
    ) as client:
        task_id, revision_id = _create_valid_revision(client, api_harness)
        response = client.post(
            f"/api/v1/dev/tasks/{task_id}/tool-runs",
            json={"task_input_revision_id": revision_id},
        )
        assert response.status_code == 504
        tool_run_id = response.json()["resource"]["tool_run_id"]
        query = client.get(f"/api/v1/dev/tool-runs/{tool_run_id}")

    assert tool_client.calls == 1
    assert response.json()["error"]["code"] == "RUNTIME_TIMEOUT"
    assert query.status_code == 200
    assert query.json()["data"]["status"] == "FAILED"
    assert query.json()["data"]["error_code"] == "RUNTIME_TIMEOUT"


def test_failure_commit_error_returns_internal_error_and_keeps_running_fact(
    api_harness,
) -> None:
    actor_id = "actor_failure_commit"
    api_harness.persist_actor(actor_id)
    tool_client = _ToolClient(ToolClientTimeoutError())
    registry = build_tool_registry(tool_client)
    commit_counter = [0]
    session_factory = create_session_factory(api_harness.engine)

    def failing_factory() -> _FailingThirdCommitUnitOfWork:
        return _FailingThirdCommitUnitOfWork(session_factory, commit_counter)

    service = ToolExecutionService(
        failing_factory,
        registry,
        clock=lambda: datetime(2026, 7, 20, 1, 0, tzinfo=timezone.utc),
        id_factory=lambda: "tool_run_failure_commit",
        seed_factory=lambda: 123,
    )

    with api_harness.create_client(
        actor_id,
        settings=_settings(api_harness, enabled=True),
        tool_registry=registry,
        tool_execution_service=service,
    ) as client:
        task_id, revision_id = _create_valid_revision(client, api_harness)
        response = client.post(
            f"/api/v1/dev/tasks/{task_id}/tool-runs",
            json={"task_input_revision_id": revision_id},
        )
        query = client.get(
            "/api/v1/dev/tool-runs/tool_run_failure_commit"
        )

    assert response.status_code == 500
    assert response.json()["error"] == {
        "code": "INTERNAL_ERROR",
        "message": "内部处理失败。",
        "details": [],
    }
    assert "RUNTIME_TIMEOUT" not in response.text
    assert "Persistence" not in response.text
    assert query.status_code == 200
    assert query.json()["data"]["status"] == "RUNNING"
    assert query.json()["data"]["error_code"] is None
    assert commit_counter == [3]
    assert tool_client.calls == 1


def test_tool_run_query_hides_other_actor_resource(api_harness) -> None:
    owner_id = "actor_owner"
    other_id = "actor_other"
    api_harness.persist_actor(owner_id)
    api_harness.persist_actor(other_id)
    registry = build_tool_registry(_ToolClient())

    with api_harness.create_client(
        owner_id,
        settings=_settings(api_harness, enabled=True),
        tool_registry=registry,
    ) as owner:
        task_id, revision_id = _create_valid_revision(owner, api_harness)
        created = owner.post(
            f"/api/v1/dev/tasks/{task_id}/tool-runs",
            json={"task_input_revision_id": revision_id},
        )
        tool_run_id = created.json()["data"]["tool_run_id"]

    with api_harness.create_client(
        other_id,
        settings=_settings(api_harness, enabled=True),
        tool_registry=registry,
    ) as other:
        response = other.get(f"/api/v1/dev/tool-runs/{tool_run_id}")

    assert response.status_code == 404
