from __future__ import annotations

import base64
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
import json
from typing import Any

import pytest
import urllib3

from materialsagent.application.tools import (
    ToolCatalogService,
    UnknownToolError,
    build_tool_registry,
)
from materialsagent.domain.ports.tool_execution import (
    ToolClientProtocolError,
    ToolClientRuntimeError,
    ToolClientTimeoutError,
    ToolClientUnavailableError,
    ToolExecutionOutput,
    ToolRequestContext,
)
from materialsagent.infrastructure.tool_clients.local_zta35g import (
    LocalZTA35GToolClientAdapter,
)


TOKEN = "adapter-test-token"
IMAGE_BYTES = b"deterministic-npy-placeholder"


@dataclass(slots=True)
class _Response:
    status: int
    data: bytes
    released: bool = False

    def release_conn(self) -> None:
        self.released = True


class _Pool:
    def __init__(self, outcomes: list[object]) -> None:
        self.outcomes = list(outcomes)
        self.calls: list[dict[str, object]] = []

    def request(self, method: str, url: str, **kwargs: object) -> _Response:
        self.calls.append({"method": method, "url": url, **kwargs})
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome  # type: ignore[return-value]


def _normalized_input(
    requested_outputs: list[str] | None = None,
) -> dict[str, object]:
    return {
        "material": "ZTA35G",
        "solution_temperature": {"value": 1000, "unit": "°C"},
        "solution_time": {"value": 3, "unit": "h"},
        "aging_temperature": {"value": 730, "unit": "°C"},
        "aging_time": {"value": 3, "unit": "h"},
        "requested_outputs": requested_outputs
        or ["sem_image", "mechanical_properties"],
    }


def _context() -> ToolRequestContext:
    return ToolRequestContext(
        request_id="req_adapter",
        conversation_id="conv_adapter",
        task_id="task_adapter",
        tool_run_id="trun_adapter",
        actor_id="actor_adapter",
        user_id=None,
        requested_at=datetime(2026, 7, 20, tzinfo=timezone.utc),
    )


def _success_payload(
    requested_outputs: list[str] | None = None,
    **overrides: object,
) -> dict[str, object]:
    outputs = requested_outputs or ["sem_image", "mechanical_properties"]
    sem_requested = "sem_image" in outputs
    mechanical_requested = "mechanical_properties" in outputs
    payload: dict[str, object] = {
        "runtime_contract_version": "1.0",
        "request_id": "req_adapter",
        "task_id": "task_adapter",
        "tool_run_id": "trun_adapter",
        "tool_id": "zta35g_sem_virtual_lab",
        "tool_version": "0.1.0",
        "schema_version": "1.0",
        "status": "SUCCEEDED",
        "requested_outputs": outputs,
        "completed_outputs": outputs,
        "failed_outputs": [],
        "data": (
            {
                "yield_strength": {"value": 650.0, "unit": "MPa"},
                "elongation": {"value": 3.2, "unit": "%"},
            }
            if mechanical_requested
            else {}
        ),
        "images": [
            {
                "image_role": (
                    "generated_sem" if sem_requested else "intermediate_sem"
                ),
                "requested_output": sem_requested,
                "dtype": "float32",
                "numpy_dtype": "<f4",
                "shape": [512, 512],
                "channel_layout": "GRAYSCALE_2D",
                "value_range": [-1.0, 1.0],
                "encoding": "base64+npy",
                "byte_order": "little",
                "array_order": "C",
                "sha256": sha256(IMAGE_BYTES).hexdigest(),
                "data_base64": base64.b64encode(IMAGE_BYTES).decode("ascii"),
            }
        ],
        "warnings": [],
        "diagnostics": [
            {
                "step": "sem_generation",
                "status": "SUCCEEDED",
                "started_at": "2026-07-20T00:00:00Z",
                "completed_at": "2026-07-20T00:00:01Z",
                "duration_ms": 1000,
                "error_code": None,
                "safe_error_message": None,
            }
        ],
        "actual_runtime_parameters": {
            "seed": 42,
            "num_samples": 1,
            "guide_scale": 2.0,
            "timesteps": 1000,
        },
        "model_bundle_id": "mock-zta35g-bundle",
        "error": None,
    }
    payload.update(overrides)
    return payload


def _runtime_error_payload(
    *,
    code: object = "RUNTIME_BUSY",
    safe_message: object = "ZTA35G Runtime 正忙。",
    retryable: object = True,
) -> dict[str, object]:
    return {
        "runtime_contract_version": "1.0",
        "error": {
            "code": code,
            "safe_message": safe_message,
            "retryable": retryable,
            "failed_step": None,
            "details": {},
        },
    }


def _json_response(payload: dict[str, object], status: int = 200) -> _Response:
    return _Response(status=status, data=json.dumps(payload).encode("utf-8"))


def _tool_with_pool(
    pool: _Pool,
    *,
    timeout_seconds: float = 2.5,
):
    adapter = LocalZTA35GToolClientAdapter(
        base_url="http://127.0.0.1:8100",
        token=TOKEN,
        timeout_seconds=timeout_seconds,
        pool=pool,
    )
    registry = build_tool_registry(adapter)
    return registry.resolve("zta35g_sem_virtual_lab"), registry


def test_registry_is_only_version_source_and_unknown_tool_is_rejected() -> None:
    pool = _Pool([_json_response(_success_payload())])
    registered, registry = _tool_with_pool(pool)

    assert registered.metadata.tool_id == "zta35g_sem_virtual_lab"
    assert registered.metadata.tool_version == "0.1.0"
    assert registered.metadata.schema_version == "1.0"
    assert registered.metadata.supported_outputs == (
        "sem_image",
        "mechanical_properties",
    )
    assert registered.metadata.enabled is True
    with pytest.raises(UnknownToolError):
        registry.resolve("unknown_tool")

    catalog = ToolCatalogService(registry).list_entries()
    assert len(catalog) == 3
    zta_catalog = next(
        item for item in catalog if item["tool_id"] == "zta35g_sem_virtual_lab"
    )
    assert set(zta_catalog) == {
        "tool_id",
        "display_name",
        "description",
        "status",
        "execution_profile",
        "confirmation_required",
        "supported_outputs",
        "limitations",
        "availability",
    }
    assert zta_catalog["execution_profile"] == "MANAGED"
    assert zta_catalog["supported_outputs"] == [
        "sem_image",
        "mechanical_properties",
    ]
    assert "schema_hash" not in zta_catalog
    assert "runtime_url" not in zta_catalog


def test_material_tool_sends_only_allowed_runtime_fields_and_maps_output() -> None:
    response = _json_response(_success_payload())
    pool = _Pool([response])
    registered, _ = _tool_with_pool(pool)
    validated = registered.tool.validate_input(_normalized_input(), seed=42)

    output = registered.tool.execute(validated, _context())

    assert isinstance(output, ToolExecutionOutput)
    assert output.status == "SUCCEEDED"
    assert output.requested_outputs == (
        "sem_image",
        "mechanical_properties",
    )
    assert output.completed_outputs == output.requested_outputs
    assert output.failed_outputs == ()
    assert output.actual_runtime_parameters == {
        "seed": 42,
        "num_samples": 1,
        "guide_scale": 2.0,
        "timesteps": 1000,
    }
    assert output.images[0].data_base64 == base64.b64encode(IMAGE_BYTES).decode(
        "ascii"
    )
    assert len(pool.calls) == 1
    call = pool.calls[0]
    assert call["method"] == "POST"
    assert call["url"] == "http://127.0.0.1:8100/internal/v1/execute"
    assert call["retries"] is False
    assert call["headers"] == {
        "Content-Type": "application/json",
        "X-ZTA35G-Runtime-Token": TOKEN,
    }
    request_payload = json.loads(call["body"])
    assert set(request_payload) == {
        "runtime_contract_version",
        "request_id",
        "task_id",
        "tool_run_id",
        "tool_id",
        "tool_version",
        "schema_version",
        "process_parameters",
        "requested_outputs",
        "runtime_parameters",
    }
    assert request_payload["request_id"] == "req_adapter"
    assert request_payload["task_id"] == "task_adapter"
    assert request_payload["tool_run_id"] == "trun_adapter"
    assert request_payload["tool_version"] == registered.metadata.tool_version
    assert request_payload["schema_version"] == registered.metadata.schema_version
    assert request_payload["process_parameters"] == {
        "solution_temperature": 1000,
        "solution_time": 3,
        "aging_temperature": 730,
        "aging_time": 3,
    }
    assert request_payload["runtime_parameters"] == {
        "seed": 42,
        "num_samples": 1,
        "guide_scale": 2.0,
        "timesteps": 1000,
    }
    forbidden = {
        "actor_id",
        "user_id",
        "conversation_id",
        "database_url",
        "object_key",
        "runtime_url",
        "token",
    }
    assert forbidden.isdisjoint(request_payload)
    assert response.released is True


def test_adapter_passes_900_seconds_to_one_unretried_request() -> None:
    pool = _Pool([_json_response(_success_payload())])
    registered, _ = _tool_with_pool(pool, timeout_seconds=900.0)
    validated = registered.tool.validate_input(_normalized_input(), seed=42)

    output = registered.tool.execute(validated, _context())

    assert output.status == "SUCCEEDED"
    assert len(pool.calls) == 1
    call = pool.calls[0]
    timeout = call["timeout"]
    assert isinstance(timeout, urllib3.Timeout)
    assert timeout.total == 900.0
    assert timeout.connect_timeout == 900.0
    read_timeout = timeout.clone()
    read_timeout.start_connect()
    assert 899.0 < read_timeout.read_timeout <= 900.0
    assert call["retries"] is False


@pytest.mark.parametrize(
    ("requested_outputs", "image_role", "requested_output", "data_fields"),
    [
        (["sem_image"], "generated_sem", True, set()),
        (
            ["mechanical_properties"],
            "intermediate_sem",
            False,
            {"yield_strength", "elongation"},
        ),
        (
            ["sem_image", "mechanical_properties"],
            "generated_sem",
            True,
            {"yield_strength", "elongation"},
        ),
    ],
)
def test_adapter_accepts_all_three_legal_request_modes(
    requested_outputs: list[str],
    image_role: str,
    requested_output: bool,
    data_fields: set[str],
) -> None:
    pool = _Pool([_json_response(_success_payload(requested_outputs))])
    registered, _ = _tool_with_pool(pool)
    validated = registered.tool.validate_input(
        _normalized_input(requested_outputs),
        seed=42,
    )

    output = registered.tool.execute(validated, _context())

    assert output.requested_outputs == tuple(requested_outputs)
    assert output.completed_outputs == tuple(requested_outputs)
    assert len(output.images) == 1
    assert output.images[0].image_role == image_role
    assert output.images[0].requested_output is requested_output
    assert set(output.data) == data_fields


@pytest.mark.parametrize(
    "payload",
    [
        _success_payload(images=[]),
        _success_payload(
            images=[
                {
                    **_success_payload()["images"][0],
                    "image_role": "intermediate_sem",
                }
            ]
        ),
        _success_payload(
            images=[
                {
                    **_success_payload()["images"][0],
                    "requested_output": False,
                }
            ]
        ),
        _success_payload(data={}),
        _success_payload(
            data={
                "yield_strength": {"value": True, "unit": "MPa"},
                "elongation": {"value": 3.2, "unit": "%"},
            }
        ),
        _success_payload(
            data={
                "yield_strength": {"value": float("inf"), "unit": "MPa"},
                "elongation": {"value": 3.2, "unit": "%"},
            }
        ),
        _success_payload(
            data={
                "yield_strength": {"value": 650.0, "unit": "MPa"},
                "elongation": {"value": "3.2", "unit": "%"},
            }
        ),
        _success_payload(
            data={
                "yield_strength": {"value": 650.0, "unit": "Pa"},
                "elongation": {"value": 3.2, "unit": "%"},
            }
        ),
        _success_payload(
            ["mechanical_properties"],
            images=[
                {
                    **_success_payload(["mechanical_properties"])["images"][0],
                    "image_role": "generated_sem",
                }
            ],
        ),
        _success_payload(
            ["mechanical_properties"],
            images=[
                {
                    **_success_payload(["mechanical_properties"])["images"][0],
                    "requested_output": True,
                }
            ],
        ),
        _success_payload(
            status="PARTIALLY_SUCCEEDED",
            completed_outputs=["sem_image"],
            failed_outputs=["mechanical_properties"],
            error={
                "code": "MECHANICAL_PROPERTY_PREDICTION_FAILED",
                "safe_message": "力学性能预测未完成。",
                "retryable": False,
                "failed_step": "mechanical_property_prediction",
                "details": {},
            },
        ),
        _success_payload(
            status="PARTIALLY_SUCCEEDED",
            completed_outputs=["mechanical_properties"],
            failed_outputs=["sem_image"],
            error={
                "code": "SEM_GENERATION_FAILED",
                "safe_message": "SEM generation did not complete.",
                "retryable": False,
                "failed_step": "sem_generation",
                "details": {},
            },
        ),
        _success_payload(
            status="PARTIALLY_SUCCEEDED",
            completed_outputs=["mechanical_properties"],
            failed_outputs=["sem_image"],
            images=[
                {
                    **_success_payload()["images"][0],
                    "image_role": "intermediate_sem",
                    "requested_output": False,
                }
            ],
            error={
                "code": "SEM_GENERATION_FAILED",
                "safe_message": "SEM generation did not complete.",
                "retryable": False,
                "failed_step": "sem_generation",
                "details": {},
            },
        ),
    ],
    ids=(
        "missing-image",
        "wrong-image-role",
        "wrong-requested-output",
        "missing-performance-data",
        "boolean-performance-value",
        "non-finite-performance-value",
        "string-performance-value",
        "wrong-performance-unit",
        "mechanical-only-wrong-image-role",
        "mechanical-only-wrong-requested-output",
        "failed-performance-with-success-data",
        "both-requested-mechanical-only-generated-image",
        "both-requested-mechanical-only-intermediate-image",
    ),
)
def test_adapter_rejects_invalid_success_output_invariants(
    payload: dict[str, object],
) -> None:
    pool = _Pool([_json_response(payload)])
    registered, _ = _tool_with_pool(pool)
    validated = registered.tool.validate_input(_normalized_input(), seed=42)

    with pytest.raises(ToolClientProtocolError):
        registered.tool.execute(validated, _context())

    assert len(pool.calls) == 1


@pytest.mark.parametrize(
    ("outcome", "error_type"),
    [
        (TimeoutError("secret timeout detail"), ToolClientTimeoutError),
        (OSError("secret connection detail"), ToolClientUnavailableError),
        (
            urllib3.exceptions.NewConnectionError(
                None,
                "secret connection detail",
            ),
            ToolClientUnavailableError,
        ),
        (_Response(status=200, data=b"not-json"), ToolClientProtocolError),
        (
            _json_response(
                _success_payload(request_id="wrong-request-id")
            ),
            ToolClientProtocolError,
        ),
        (
            _Response(status=200, data=b"{" + (b"x" * (4 * 1024 * 1024))),
            ToolClientProtocolError,
        ),
    ],
)
def test_adapter_maps_transport_and_protocol_errors_without_retry(
    outcome: object,
    error_type: type[Exception],
) -> None:
    pool = _Pool([outcome])
    registered, _ = _tool_with_pool(pool)
    validated = registered.tool.validate_input(_normalized_input(), seed=42)

    with pytest.raises(error_type) as exc_info:
        registered.tool.execute(validated, _context())

    assert len(pool.calls) == 1
    assert "secret" not in str(exc_info.value)
    assert TOKEN not in str(exc_info.value)
    assert base64.b64encode(IMAGE_BYTES).decode("ascii") not in str(
        exc_info.value
    )


def test_adapter_maps_busy_error_and_preserves_retryable_without_retry() -> None:
    pool = _Pool(
        [
            _json_response(
                _runtime_error_payload(),
                status=503,
            )
        ]
    )
    registered, _ = _tool_with_pool(pool)
    validated = registered.tool.validate_input(_normalized_input(), seed=42)

    with pytest.raises(ToolClientRuntimeError) as exc_info:
        registered.tool.execute(validated, _context())

    assert exc_info.value.code == "RUNTIME_BUSY"
    assert exc_info.value.retryable is True
    assert len(pool.calls) == 1


@pytest.mark.parametrize(
    "payload",
    [
        _runtime_error_payload(code="UNKNOWN_RUNTIME_CODE"),
        _runtime_error_payload(safe_message="   "),
        _runtime_error_payload(safe_message="x" * 257),
        _runtime_error_payload(safe_message="unsafe\nmessage"),
        _runtime_error_payload(retryable=1),
    ],
    ids=(
        "unknown-code",
        "blank-safe-message",
        "overlong-safe-message",
        "non-printable-safe-message",
        "non-boolean-retryable",
    ),
)
def test_adapter_rejects_invalid_runtime_error_envelopes(
    payload: dict[str, object],
) -> None:
    pool = _Pool([_json_response(payload, status=503)])
    registered, _ = _tool_with_pool(pool)
    validated = registered.tool.validate_input(_normalized_input(), seed=42)

    with pytest.raises(ToolClientProtocolError):
        registered.tool.execute(validated, _context())

    assert len(pool.calls) == 1


@pytest.mark.parametrize(
    ("code", "retryable"),
    [
        ("RUNTIME_BUSY", True),
        ("RUNTIME_NOT_READY", True),
        ("MODEL_LOAD_FAILED", False),
        ("SEM_GENERATION_FAILED", False),
        ("INTERNAL_RUNTIME_ERROR", False),
    ],
)
def test_adapter_accepts_allowlisted_runtime_errors(
    code: str,
    retryable: bool,
) -> None:
    pool = _Pool(
        [
            _json_response(
                _runtime_error_payload(
                    code=code,
                    safe_message="Runtime 安全错误。",
                    retryable=retryable,
                ),
                status=503 if retryable else 500,
            )
        ]
    )
    registered, _ = _tool_with_pool(pool)
    validated = registered.tool.validate_input(_normalized_input(), seed=42)

    with pytest.raises(ToolClientRuntimeError) as raised:
        registered.tool.execute(validated, _context())

    assert raised.value.code == code
    assert raised.value.retryable is retryable
    assert len(pool.calls) == 1


def test_catalog_health_uses_ready_without_execute_and_filters_internal_details() -> None:
    ready_response = _json_response(
        {
            "runtime_contract_version": "1.0",
            "status": "READY",
            "model_files": {"status": "AVAILABLE"},
            "model_loaded": True,
            "device": {"status": "AVAILABLE", "kind": "cpu"},
            "can_accept_execution": True,
            "busy": False,
            "supported_tool": {
                "tool_id": "zta35g_sem_virtual_lab",
                "tool_version": "0.1.0",
                "schema_version": "1.0",
            },
            "model_bundle_id": "secret-internal-bundle",
            "checked_at": "2026-07-20T00:00:00Z",
            "error": None,
        }
    )
    pool = _Pool([ready_response])
    _, registry = _tool_with_pool(pool)

    entry = ToolCatalogService(registry).get_entry(
        "zta35g_sem_virtual_lab"
    )

    assert entry["availability"] == "AVAILABLE"
    assert len(pool.calls) == 1
    assert pool.calls[0]["method"] == "GET"
    assert pool.calls[0]["url"].endswith("/internal/v1/health/ready")
    assert "model_bundle_id" not in entry
    assert "device" not in entry
