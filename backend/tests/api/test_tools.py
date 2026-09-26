from __future__ import annotations

from fastapi.testclient import TestClient
import pytest

from materialsagent.application.tools import build_tool_registry
from materialsagent.domain.ports.tool_execution import ToolExecutionOutput
from materialsagent.infrastructure.config import load_settings
from materialsagent.main import create_app


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


@pytest.mark.parametrize("dev_routes_enabled", [False, True], ids=["disabled", "enabled"])
def test_retired_tool_execution_route_is_absent(dev_routes_enabled) -> None:
    # Route registration is independent of persistence; never provision a DB here.
    settings = load_settings({}).model_copy(
        update={"m5_dev_routes_enabled": dev_routes_enabled}
    )
    app = create_app(settings=settings)
    with TestClient(app) as client:
        response = client.post(
            "/api/v1/dev/tasks/task_hidden/tool-runs",
            json={"task_input_revision_id": "revision_hidden"},
        )

    assert response.status_code == 404
    assert "/api/v1/dev/tasks/{task_id}/tool-runs" not in app.openapi()["paths"]
