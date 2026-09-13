from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import func, select

from materialsagent.application.tool_execution import ToolExecutionService
from materialsagent.application.tools import build_tool_registry
from materialsagent.domain.ports.tool_execution import (
    ToolClientTimeoutError,
    ToolExecutionOutput,
)
from materialsagent.infrastructure.db.conversation_task import (
    TaskInputRevisionRow,
)
from materialsagent.infrastructure.db.asset import AssetRow
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
    conversation = client.post(
        "/api/v1/conversations",
        headers={"Idempotency-Key": f"tools-conversation-{uuid4().hex}"},
        json={},
    )
    assert conversation.status_code == 201
    conversation_id = conversation.json()["data"]["conversation_id"]
    message = client.post(
        f"/api/v1/conversations/{conversation_id}/messages",
        headers={"Idempotency-Key": "tool-valid-revision"},
        json={
            "submission_mode": "NEW_TASK",
            "content_text": "完整合法 Tool 请求",
        },
    )
    assert message.status_code == 200
    task_id = message.json()["data"]["task"]["task_id"]
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
    entries = {
        item["tool_id"]: item for item in listing.json()["data"]
    }
    assert set(entries) == {
        "materials_unit_conversion",
        "zta35g_sem_virtual_lab",
        "ebsd_yield_strength_predictor",
    }
    entry = entries["zta35g_sem_virtual_lab"]
    assert set(entry) == {
        "tool_id",
        "display_name",
        "description",
        "status",
        "execution_profile",
        "confirmation_required",
        "availability",
        "supported_outputs",
        "limitations",
    }
    assert entry["tool_id"] == "zta35g_sem_virtual_lab"
    assert entry["status"] == "ACTIVE"
    assert entry["execution_profile"] == "MANAGED"
    assert entry["confirmation_required"] is False
    assert entry["availability"] == "AVAILABLE"
    assert "version" not in entry
    assert "schema_hash" not in entry
    assert "runtime_url" not in entry
    assert entries["materials_unit_conversion"]["execution_profile"] == "STANDARD"
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












def test_retired_tool_execution_route_is_absent_even_when_dev_flag_set(api_harness):
    api_harness.persist_actor("no-legacy")
    with api_harness.create_client("no-legacy", settings=api_harness.settings.model_copy(update={"m5_dev_routes_enabled":True})) as client:
        assert client.post("/api/v1/dev/tasks/task/tool-runs",json={"task_input_revision_id":"revision"}).status_code == 404
