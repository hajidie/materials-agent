from __future__ import annotations

from datetime import timedelta

import pytest

from backend.tests.integration.db.test_result_commit import (
    ACTOR,
    BASE,
    _factory,
    _seed,
)
from materialsagent.application.result_service import ResultService


def _persist_result(
    api_harness,
    *,
    requested: tuple[str, ...],
    completed: tuple[str, ...],
    failed: tuple[str, ...],
):
    receipt, assets = _seed(
        api_harness.engine,
        requested=requested,
        completed=completed,
        failed=failed,
    )
    return ResultService(
        _factory(api_harness.engine),
        clock=lambda: BASE + timedelta(seconds=5),
        id_factory=lambda: "result_1",
    ).commit_result(ACTOR, receipt=receipt, assets=assets)


def test_get_tool_result_uses_linked_asset_public_projection(
    api_harness,
) -> None:
    _persist_result(
        api_harness,
        requested=("sem_image", "mechanical_properties"),
        completed=("sem_image", "mechanical_properties"),
        failed=(),
    )

    with api_harness.create_client("actor_1") as client:
        response = client.get("/api/v1/tool-results/result_1")

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["result_id"] == "result_1"
    assert data["status"] == "SUCCEEDED"
    assert data["schema_hash"] == (
        "f821240f782ce788bc723fd1acd02a2e58cedbf68b70b1414e2accd16d989d07"
    )
    assert "schema_version" not in data
    assert data["data"]["yield_strength"] == {
        "value": 650.0,
        "unit": "MPa",
    }
    assert data["artifacts"] == [
        {
            "asset_id": "asset_1",
            "status": "AVAILABLE",
            "role": "requested_output",
            "media_type": "image/png",
            "width": 512,
            "height": 512,
            "bit_depth": 8,
            "size_bytes": 1480,
            "sha256": "c" * 64,
            "content_url": "/api/v1/assets/asset_1/content",
        }
    ]
    assert data["provenance"] == {
        "input_revision": 1,
        "normalized_process_parameters": {
            "solution_temperature": {
                "value": 1000,
                "unit": "°C",
            },
            "solution_time": {"value": 3, "unit": "h"},
            "aging_temperature": {
                "value": 730,
                "unit": "°C",
            },
            "aging_time": {"value": 3, "unit": "h"},
        },
        "actual_runtime_parameters": {
            "seed": 101,
            "num_samples": 1,
            "guide_scale": 2.0,
            "timesteps": 1000,
        },
    }
    assert "task_input_revision_id" not in response.text
    assert "model_bundle_id" not in response.text
    assert "object_key" not in response.text
    assert "internal-bundle" not in response.text
    assert "9000" not in response.text


@pytest.mark.parametrize(
    ("requested", "completed", "failed", "status"),
    [
        (
            ("sem_image", "mechanical_properties"),
            ("sem_image",),
            ("mechanical_properties",),
            "PARTIALLY_SUCCEEDED",
        ),
        (
            ("mechanical_properties",),
            (),
            ("mechanical_properties",),
            "FAILED",
        ),
    ],
)
def test_partial_and_failed_tool_results_are_publicly_queryable(
    api_harness,
    requested: tuple[str, ...],
    completed: tuple[str, ...],
    failed: tuple[str, ...],
    status: str,
) -> None:
    _persist_result(
        api_harness,
        requested=requested,
        completed=completed,
        failed=failed,
    )

    with api_harness.create_client("actor_1") as client:
        response = client.get("/api/v1/tool-results/result_1")

    assert response.status_code == 200
    assert response.json()["data"]["status"] == status


def test_missing_and_foreign_tool_results_share_safe_404(
    api_harness,
) -> None:
    _persist_result(
        api_harness,
        requested=("sem_image",),
        completed=("sem_image",),
        failed=(),
    )
    api_harness.persist_actor("actor_2")

    with api_harness.create_client("actor_2") as client:
        foreign = client.get("/api/v1/tool-results/result_1")
        missing = client.get("/api/v1/tool-results/missing")

    assert foreign.status_code == missing.status_code == 404
    assert foreign.json()["error"]["code"] == "RESOURCE_NOT_FOUND"
    assert missing.json()["error"]["code"] == "RESOURCE_NOT_FOUND"
