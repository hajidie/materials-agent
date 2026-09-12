from __future__ import annotations

import base64
from hashlib import sha256
import json
import math
from typing import Any, Final
from urllib.parse import urlparse

import urllib3

from materialsagent.domain.ports.tool_execution import (
    ToolClientProtocolError,
    ToolClientRuntimeError,
    ToolClientTimeoutError,
    ToolClientUnavailableError,
    ToolExecutionInput,
    ToolExecutionOutput,
    ToolImagePayload,
    ToolMetadata,
    ToolRequestContext,
)


MAX_REQUEST_BYTES: Final = 64 * 1024
MAX_RESPONSE_BYTES: Final = 4 * 1024 * 1024
RUNTIME_CONTRACT_VERSION: Final = "1.0"
MAX_SAFE_MESSAGE_CHARS: Final = 256
RUNTIME_ERROR_CODES: Final = frozenset(
    {
        "INVALID_RUNTIME_REQUEST",
        "UNSUPPORTED_TOOL",
        "SCHEMA_VERSION_MISMATCH",
        "TOOL_VERSION_MISMATCH",
        "RUNTIME_NOT_READY",
        "RUNTIME_BUSY",
        "MODEL_LOAD_FAILED",
        "SEM_GENERATION_FAILED",
        "MECHANICAL_PROPERTY_PREDICTION_FAILED",
        "INVALID_MODEL_OUTPUT",
        "INTERNAL_RUNTIME_ERROR",
    }
)
RUNTIME_FAILED_STEPS: Final = frozenset(
    {
        "model_loading",
        "sem_generation",
        "mechanical_property_prediction",
    }
)


class LocalZTA35GToolClientAdapter:
    def __init__(
        self,
        *,
        base_url: str,
        token: str,
        timeout_seconds: float,
        pool: Any | None = None,
    ) -> None:
        self._base_url = _validated_base_url(base_url)
        if not isinstance(token, str) or not token:
            raise ValueError("Runtime token is required.")
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or timeout_seconds <= 0
        ):
            raise ValueError("Runtime timeout is invalid.")
        self._token = token
        self._timeout = urllib3.Timeout(total=float(timeout_seconds))
        self._pool = pool or urllib3.PoolManager(num_pools=1)

    def _request_timeout(self):
        from materialsagent.application.execution_deadline import remaining_timeout
        return urllib3.Timeout(total=remaining_timeout(float(self._timeout.total)))

    def execute(
        self,
        metadata: ToolMetadata,
        validated_input: ToolExecutionInput,
        request_context: ToolRequestContext,
    ) -> ToolExecutionOutput:
        body = {
            "runtime_contract_version": RUNTIME_CONTRACT_VERSION,
            "request_id": request_context.request_id,
            "task_id": request_context.task_id,
            "tool_run_id": request_context.tool_run_id,
            "tool_id": metadata.tool_id,
            "tool_version": metadata.tool_version,
            "schema_version": metadata.schema_version,
            **validated_input.to_json(),
        }
        payload = self._request_json("POST", "/internal/v1/execute", body)
        return _parse_output(
            payload,
            metadata=metadata,
            validated_input=validated_input,
            request_context=request_context,
        )

    def readiness(self, metadata: ToolMetadata) -> str:
        try:
            payload = self._request_json(
                "GET",
                "/internal/v1/health/ready",
                None,
            )
            if payload.get("runtime_contract_version") != RUNTIME_CONTRACT_VERSION:
                return "UNAVAILABLE"
            supported = payload.get("supported_tool")
            if not isinstance(supported, dict) or (
                supported.get("tool_id"),
                supported.get("tool_version"),
                supported.get("schema_version"),
            ) != (
                metadata.tool_id,
                metadata.tool_version,
                metadata.schema_version,
            ):
                return "UNAVAILABLE"
            if payload.get("status") != "READY" or payload.get("model_loaded") is not True:
                return "UNAVAILABLE"
            if payload.get("busy") is True or payload.get("can_accept_execution") is False:
                return "DEGRADED"
            return "AVAILABLE"
        except Exception:
            return "UNAVAILABLE"

    def _request_json(
        self,
        method: str,
        path: str,
        payload: dict[str, object] | None,
    ) -> dict[str, object]:
        body: str | None = None
        headers = {"X-ZTA35G-Runtime-Token": self._token}
        if payload is not None:
            body = json.dumps(
                payload,
                ensure_ascii=False,
                separators=(",", ":"),
            )
            if len(body.encode("utf-8")) > MAX_REQUEST_BYTES:
                raise ToolClientProtocolError()
            headers = {"Content-Type": "application/json", **headers}
        try:
            response = self._pool.request(
                method,
                f"{self._base_url}{path}",
                body=body,
                headers=headers,
                timeout=self._request_timeout(),
                retries=False,
                preload_content=True,
            )
        except urllib3.exceptions.NewConnectionError:
            raise ToolClientUnavailableError() from None
        except (TimeoutError, urllib3.exceptions.TimeoutError):
            raise ToolClientTimeoutError() from None
        except (OSError, urllib3.exceptions.HTTPError):
            raise ToolClientUnavailableError() from None
        try:
            response_bytes = response.data
            if not isinstance(response_bytes, bytes) or len(response_bytes) > MAX_RESPONSE_BYTES:
                raise ToolClientProtocolError()
            try:
                decoded = json.loads(response_bytes.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                raise ToolClientProtocolError() from None
            if not isinstance(decoded, dict):
                raise ToolClientProtocolError()
            if response.status != 200:
                raise _runtime_error(decoded)
            return decoded
        finally:
            response.release_conn()


def _validated_base_url(value: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError("Runtime URL is invalid.")
    try:
        parsed = urlparse(value)
        port = parsed.port
    except ValueError:
        raise ValueError("Runtime URL is invalid.") from None
    if (
        parsed.scheme != "http"
        or parsed.hostname != "127.0.0.1"
        or port is None
        or not 1 <= port <= 65535
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.params
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("Runtime URL is invalid.")
    return value.rstrip("/")


def _safe_error_details(value: object) -> bool:
    if not isinstance(value, dict) or len(value) > 10:
        return False
    for key, item in value.items():
        if (
            not isinstance(key, str)
            or not key
            or len(key) > 64
            or not key.isprintable()
        ):
            return False
        if item is None or isinstance(item, bool):
            continue
        if isinstance(item, int):
            continue
        if isinstance(item, float):
            if math.isfinite(item):
                continue
            return False
        if isinstance(item, str):
            if item and len(item) <= MAX_SAFE_MESSAGE_CHARS and item.isprintable():
                continue
            return False
        return False
    return True


def _validated_runtime_error(value: object) -> dict[str, object]:
    required = {"code", "safe_message", "retryable", "failed_step", "details"}
    if not isinstance(value, dict) or set(value) != required:
        raise ToolClientProtocolError()
    code = value.get("code")
    safe_message = value.get("safe_message")
    retryable = value.get("retryable")
    failed_step = value.get("failed_step")
    if (
        not isinstance(code, str)
        or code not in RUNTIME_ERROR_CODES
        or not isinstance(safe_message, str)
        or not safe_message.strip()
        or len(safe_message) > MAX_SAFE_MESSAGE_CHARS
        or not safe_message.isprintable()
        or not isinstance(retryable, bool)
        or (code == "RUNTIME_BUSY" and retryable is not True)
        or (
            failed_step is not None
            and failed_step not in RUNTIME_FAILED_STEPS
        )
        or not _safe_error_details(value.get("details"))
    ):
        raise ToolClientProtocolError()
    return dict(value)


def _runtime_error(payload: dict[str, object]) -> ToolClientRuntimeError:
    if payload.get("runtime_contract_version") != RUNTIME_CONTRACT_VERSION:
        raise ToolClientProtocolError()
    error = _validated_runtime_error(payload.get("error"))
    return ToolClientRuntimeError(
        code=error["code"],
        safe_message=error["safe_message"],
        retryable=error["retryable"],
    )


def _string_tuple(value: object) -> tuple[str, ...]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ToolClientProtocolError()
    return tuple(value)


def _parse_image(value: object) -> ToolImagePayload:
    if not isinstance(value, dict):
        raise ToolClientProtocolError()
    required = {
        "image_role",
        "requested_output",
        "dtype",
        "numpy_dtype",
        "shape",
        "channel_layout",
        "value_range",
        "encoding",
        "byte_order",
        "array_order",
        "sha256",
        "data_base64",
    }
    if set(value) != required:
        raise ToolClientProtocolError()
    data_base64 = value.get("data_base64")
    if not isinstance(data_base64, str):
        raise ToolClientProtocolError()
    try:
        decoded = base64.b64decode(data_base64, validate=True)
    except (ValueError, base64.binascii.Error):
        raise ToolClientProtocolError() from None
    digest = value.get("sha256")
    if digest is not None and (
        not isinstance(digest, str) or sha256(decoded).hexdigest() != digest
    ):
        raise ToolClientProtocolError()
    if (
        value.get("image_role") not in {"generated_sem", "intermediate_sem"}
        or not isinstance(value.get("requested_output"), bool)
        or value.get("dtype") != "float32"
        or value.get("numpy_dtype") != "<f4"
        or value.get("shape") != [512, 512]
        or value.get("channel_layout") != "GRAYSCALE_2D"
        or value.get("value_range") != [-1.0, 1.0]
        or value.get("encoding") != "base64+npy"
        or value.get("byte_order") != "little"
        or value.get("array_order") != "C"
    ):
        raise ToolClientProtocolError()
    return ToolImagePayload(
        image_role=value["image_role"],
        requested_output=value["requested_output"],
        dtype="float32",
        numpy_dtype="<f4",
        shape=(512, 512),
        channel_layout="GRAYSCALE_2D",
        value_range=(-1.0, 1.0),
        encoding="base64+npy",
        byte_order="little",
        array_order="C",
        sha256=digest,
        data_base64=data_base64,
    )


def _validate_performance_data(
    data: dict[str, object],
    *,
    completed_outputs: tuple[str, ...],
) -> None:
    if "mechanical_properties" not in completed_outputs:
        if data:
            raise ToolClientProtocolError()
        return
    if set(data) != {"yield_strength", "elongation"}:
        raise ToolClientProtocolError()
    for field_name, expected_unit in (
        ("yield_strength", "MPa"),
        ("elongation", "%"),
    ):
        result = data.get(field_name)
        if not isinstance(result, dict) or set(result) != {"value", "unit"}:
            raise ToolClientProtocolError()
        value = result.get("value")
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or result.get("unit") != expected_unit
        ):
            raise ToolClientProtocolError()


def _parse_output(
    payload: dict[str, object],
    *,
    metadata: ToolMetadata,
    validated_input: ToolExecutionInput,
    request_context: ToolRequestContext,
) -> ToolExecutionOutput:
    required = {
        "runtime_contract_version",
        "request_id",
        "task_id",
        "tool_run_id",
        "tool_id",
        "tool_version",
        "schema_version",
        "status",
        "requested_outputs",
        "completed_outputs",
        "failed_outputs",
        "data",
        "images",
        "warnings",
        "diagnostics",
        "actual_runtime_parameters",
        "model_bundle_id",
        "error",
    }
    if set(payload) != required or (
        payload.get("runtime_contract_version"),
        payload.get("request_id"),
        payload.get("task_id"),
        payload.get("tool_run_id"),
        payload.get("tool_id"),
        payload.get("tool_version"),
        payload.get("schema_version"),
    ) != (
        RUNTIME_CONTRACT_VERSION,
        request_context.request_id,
        request_context.task_id,
        request_context.tool_run_id,
        metadata.tool_id,
        metadata.tool_version,
        metadata.schema_version,
    ):
        raise ToolClientProtocolError()
    requested = _string_tuple(payload.get("requested_outputs"))
    completed = _string_tuple(payload.get("completed_outputs"))
    failed = _string_tuple(payload.get("failed_outputs"))
    if (
        requested != validated_input.requested_outputs
        or len(set(completed)) != len(completed)
        or len(set(failed)) != len(failed)
        or set(completed) & set(failed)
        or set(completed) | set(failed) != set(requested)
    ):
        raise ToolClientProtocolError()
    status = payload.get("status")
    if status not in {"SUCCEEDED", "PARTIALLY_SUCCEEDED", "FAILED"}:
        raise ToolClientProtocolError()
    if (
        (status == "SUCCEEDED" and (completed != requested or failed))
        or (status == "PARTIALLY_SUCCEEDED" and (not completed or not failed))
        or (status == "FAILED" and completed)
    ):
        raise ToolClientProtocolError()
    data = payload.get("data")
    images = payload.get("images")
    warnings = payload.get("warnings")
    diagnostics = payload.get("diagnostics")
    actual = payload.get("actual_runtime_parameters")
    error = payload.get("error")
    model_bundle_id = payload.get("model_bundle_id")
    if (
        not isinstance(data, dict)
        or not isinstance(images, list)
        or len(images) > 1
        or not isinstance(warnings, list)
        or len(warnings) > 20
        or any(not isinstance(item, dict) for item in warnings)
        or not isinstance(diagnostics, list)
        or len(diagnostics) > 10
        or any(not isinstance(item, dict) for item in diagnostics)
        or not isinstance(actual, dict)
        or actual != validated_input.runtime_parameters
        or (model_bundle_id is not None and not isinstance(model_bundle_id, str))
    ):
        raise ToolClientProtocolError()
    parsed_error = (
        None if error is None else _validated_runtime_error(error)
    )
    if (status == "SUCCEEDED" and parsed_error is not None) or (
        status != "SUCCEEDED" and parsed_error is None
    ):
        raise ToolClientProtocolError()
    parsed_images = tuple(_parse_image(image) for image in images)
    if status in {"SUCCEEDED", "PARTIALLY_SUCCEEDED"} and len(parsed_images) != 1:
        raise ToolClientProtocolError()
    if status in {"SUCCEEDED", "PARTIALLY_SUCCEEDED"}:
        image = parsed_images[0]
        if "sem_image" in completed:
            expected_image = ("generated_sem", True)
        elif requested == ("mechanical_properties",):
            expected_image = ("intermediate_sem", False)
        else:
            raise ToolClientProtocolError()
        if (image.image_role, image.requested_output) != expected_image:
            raise ToolClientProtocolError()
    _validate_performance_data(data, completed_outputs=completed)
    return ToolExecutionOutput(
        status=status,
        requested_outputs=requested,
        completed_outputs=completed,
        failed_outputs=failed,
        data=dict(data),
        images=parsed_images,
        warnings=tuple(dict(item) for item in warnings),
        diagnostics=tuple(dict(item) for item in diagnostics),
        actual_runtime_parameters=dict(actual),
        model_bundle_id=model_bundle_id,
        error=parsed_error,
    )
