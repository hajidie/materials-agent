from __future__ import annotations

from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timezone
import json
from typing import Any

import pytest


BASE_TIME = datetime(2026, 7, 23, 12, 0, tzinfo=timezone.utc)
SCHEMA_HASH = "f821240f782ce788bc723fd1acd02a2e58cedbf68b70b1414e2accd16d989d07"


def _result_type() -> type[Any]:
    from materialsagent.domain.models.tool_result import ToolResult

    return ToolResult


def _result(
    *,
    status: str = "SUCCEEDED",
    requested_outputs: list[str] | None = None,
    completed_outputs: list[str] | None = None,
    failed_outputs: list[str] | None = None,
    data: dict[str, object] | None = None,
    **overrides: object,
) -> object:
    ToolResult = _result_type()
    if requested_outputs is None:
        requested_outputs = ["sem_image", "mechanical_properties"]
    if completed_outputs is None:
        completed_outputs = (
            ["sem_image", "mechanical_properties"]
            if status == "SUCCEEDED"
            else ["sem_image"] if status == "PARTIALLY_SUCCEEDED" else []
        )
    if failed_outputs is None:
        failed_outputs = (
            []
            if status == "SUCCEEDED"
            else ["mechanical_properties"]
            if status == "PARTIALLY_SUCCEEDED"
            else list(requested_outputs)
        )
    if data is None:
        data = (
            {
                "yield_strength": {"value": 1010.5, "unit": "MPa"},
                "elongation": {"value": 8.2, "unit": "%"},
            }
            if "mechanical_properties" in completed_outputs
            else {}
        )
    values = {
        "result_id": "result_domain",
        "task_id": "task_domain",
        "tool_run_id": "tool_run_domain",
        "actor_id": "actor_domain",
        "status": status,
        "requested_outputs": requested_outputs,
        "completed_outputs": completed_outputs,
        "failed_outputs": failed_outputs,
        "data": data,
        "warnings": [],
        "provenance": {
            "input_revision": 1,
            "normalized_process_parameters": {},
            "actual_runtime_parameters": {},
        },
        "error": (
            {
                "code": "RESULT_FAILED",
                "safe_message": "Tool output failed.",
                "retryable": False,
            }
            if status != "SUCCEEDED"
            else None
        ),
        "tool_id": "zta35g_sem_virtual_lab",
        "tool_version": "0.1.0",
        "schema_hash": SCHEMA_HASH,
        "created_at": BASE_TIME,
    }
    values.update(overrides)
    return ToolResult(**values)


@pytest.mark.parametrize(
    ("status", "completed", "failed"),
    [
        ("SUCCEEDED", ["sem_image", "mechanical_properties"], []),
        ("PARTIALLY_SUCCEEDED", ["sem_image"], ["mechanical_properties"]),
        ("FAILED", [], ["sem_image", "mechanical_properties"]),
    ],
)
def test_terminal_result_statuses_have_mechanically_consistent_outputs(
    status: str,
    completed: list[str],
    failed: list[str],
) -> None:
    result = _result(status=status, completed_outputs=completed, failed_outputs=failed)

    assert result.status == status
    assert result.completed_outputs == tuple(completed)
    assert result.failed_outputs == tuple(failed)


def test_requested_outputs_are_deduplicated_and_all_output_orders_follow_request() -> None:
    result = _result(
        status="PARTIALLY_SUCCEEDED",
        requested_outputs=["mechanical_properties", "sem_image", "mechanical_properties"],
        completed_outputs=["sem_image"],
        failed_outputs=["mechanical_properties"],
    )

    assert result.requested_outputs == ("mechanical_properties", "sem_image")
    assert result.completed_outputs == ("sem_image",)
    assert result.failed_outputs == ("mechanical_properties",)


@pytest.mark.parametrize(
    "changes",
    [
        {"requested_outputs": []},
        {"requested_outputs": ["unknown_output"]},
        {"completed_outputs": ["sem_image"], "failed_outputs": ["sem_image"]},
        {"completed_outputs": ["sem_image"], "failed_outputs": []},
        {"completed_outputs": ["unknown_output"], "failed_outputs": ["mechanical_properties"]},
        {"status": "SUCCEEDED", "completed_outputs": ["sem_image"], "failed_outputs": ["mechanical_properties"]},
    ],
)
def test_result_rejects_invalid_output_sets_or_status_mismatches(
    changes: dict[str, object],
) -> None:
    with pytest.raises(ValueError):
        _result(**changes)


@pytest.mark.parametrize(
    "data",
    [
        {"unexpected": 1},
        {"yield_strength": {"value": float("nan"), "unit": "MPa"}, "elongation": {"value": 8.2, "unit": "%"}},
        {"yield_strength": {"value": 1010, "unit": "MPa"}, "elongation": {"value": 8.2, "unit": "percent"}},
    ],
)
def test_result_rejects_nonfinite_or_uncontrolled_mechanical_data(
    data: dict[str, object],
) -> None:
    with pytest.raises(ValueError, match="data"):
        _result(data=data)


def test_result_requires_empty_data_without_completed_mechanical_properties() -> None:
    with pytest.raises(ValueError, match="data"):
        _result(
            status="FAILED",
            completed_outputs=[],
            failed_outputs=["sem_image", "mechanical_properties"],
            data={"yield_strength": {"value": 1010, "unit": "MPa"}},
        )


def test_result_provenance_uses_public_revision_number_and_normalized_parameters() -> None:
    provenance = {
        "input_revision": 1,
        "normalized_process_parameters": {
            "solution_temperature": {
                "value": 1000,
                "unit": "°C",
            },
        },
        "actual_runtime_parameters": {"seed": 101},
    }

    result = _result(provenance=provenance)

    assert result.provenance == provenance
    assert "task_input_revision_id" not in result.provenance


def test_result_requires_product_schema_hash_provenance() -> None:
    result = _result()

    assert result.schema_hash == SCHEMA_HASH
    with pytest.raises(ValueError, match="schema_hash"):
        _result(schema_hash="1.0")


@pytest.mark.parametrize("input_revision", [True, 0, -1, "1"])
def test_result_provenance_requires_positive_integer_revision(
    input_revision: object,
) -> None:
    with pytest.raises(ValueError, match="provenance"):
        _result(
            provenance={
                "input_revision": input_revision,
                "normalized_process_parameters": {},
                "actual_runtime_parameters": {},
            }
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("warnings", [{"value": float("inf")}]),
        ("provenance", {"internal_path": "C:/private/model.pt"}),
        ("provenance", {"object_key": "assets/private.png"}),
        ("provenance", {"bucket": "private-bucket"}),
        ("provenance", {"minio_endpoint": "http://127.0.0.1:9000"}),
        ("provenance", {"model_bundle_id": "internal-bundle"}),
        ("provenance", {"weight_fingerprint": "f" * 64}),
        ("error", {"safe_message": "failure", "traceback": "SECRET"}),
        ("warnings", ["x" * 4097]),
        ("provenance", {"nested": {"too": {"deep": {"again": {"no": 1}}}}}),
    ],
)
def test_result_json_metadata_rejects_nonfinite_internal_or_unbounded_values(
    field: str,
    value: object,
) -> None:
    with pytest.raises(ValueError, match=field):
        _result(**{field: value})


@pytest.mark.parametrize("escaped_character", ['"', "\\"])
def test_result_json_size_counts_encoded_escape_bytes(
    escaped_character: str,
) -> None:
    warnings = [escaped_character * 700] * 3
    encoded = json.dumps(
        warnings,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    assert len(encoded) > 4096

    with pytest.raises(ValueError, match="warnings"):
        _result(warnings=warnings)


def test_result_json_size_counts_multibyte_utf8() -> None:
    warnings = ["中" * 1024]
    encoded = json.dumps(
        warnings,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    assert len(encoded) == 3076

    result = _result(warnings=warnings)

    assert result.warnings == tuple(warnings)


def test_result_json_size_accepts_exact_limit_and_rejects_one_byte_over() -> None:
    exact = ["x" * 1024, "x" * 1024, "x" * 1024, "x" * 1011]
    over = [*exact[:-1], "x" * 1012]
    encoded_exact = json.dumps(
        exact,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    encoded_over = json.dumps(
        over,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    assert len(encoded_exact) == 4096
    assert len(encoded_over) == 4097

    result = _result(warnings=exact)

    assert result.warnings == tuple(exact)
    with pytest.raises(ValueError, match="warnings"):
        _result(warnings=over)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        (
            "data",
            {f"field_{index:02d}": "x" * 64 for index in range(64)},
        ),
        ("warnings", ["x" * 64 for _ in range(64)]),
        (
            "provenance",
            {f"field_{index:02d}": "x" * 64 for index in range(64)},
        ),
        (
            "error",
            {f"field_{index:02d}": "x" * 64 for index in range(64)},
        ),
    ],
)
def test_each_result_json_field_enforces_encoded_size_limit(
    field: str,
    value: object,
) -> None:
    with pytest.raises(ValueError, match=field):
        _result(**{field: value})


def test_result_is_frozen_and_deeply_immutable() -> None:
    result = _result(warnings=[{"code": "NOTE", "items": ["a"]}])

    with pytest.raises(FrozenInstanceError):
        result.status = "FAILED"
    with pytest.raises(TypeError):
        result.provenance["source"] = "mutated"
    with pytest.raises((AttributeError, TypeError)):
        result.warnings[0]["items"].append("late")


def test_result_asset_link_requires_safe_ids_nonnegative_order_and_utc_time() -> None:
    from materialsagent.domain.models.result_asset_link import ResultAssetLink

    link = ResultAssetLink(
        result_id="result_domain",
        asset_id="asset_domain",
        artifact_order=0,
        created_at=BASE_TIME,
    )

    assert link.artifact_order == 0
    with pytest.raises(ValueError, match="asset_id"):
        ResultAssetLink("result_domain", " ", 0, BASE_TIME)
    with pytest.raises(ValueError, match="artifact_order"):
        ResultAssetLink("result_domain", "asset_domain", True, BASE_TIME)
    with pytest.raises(ValueError, match="created_at"):
        ResultAssetLink(
            "result_domain",
            "asset_domain",
            0,
            BASE_TIME.replace(tzinfo=None),
        )
