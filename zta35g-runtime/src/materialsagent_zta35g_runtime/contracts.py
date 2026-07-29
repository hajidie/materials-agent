from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import base64
import binascii
from hashlib import sha256
import json
import math
import re
from typing import Any, Dict, Mapping, Optional, Tuple

from .constants import (
    DIAGNOSTIC_STEPS,
    GUIDE_SCALE,
    MAX_DIAGNOSTICS,
    MAX_ID_CHARS,
    MAX_REQUEST_BYTES,
    MAX_RESPONSE_BYTES,
    MAX_SAFE_DETAIL_KEY_CHARS,
    MAX_SAFE_DETAILS,
    MAX_SAFE_MESSAGE_CHARS,
    MAX_WARNINGS,
    MODEL_BUNDLE_ID,
    NUM_SAMPLES,
    PROCESS_PARAMETER_ORDER,
    PROCESS_PARAMETER_RANGES,
    RUNTIME_CONTRACT_VERSION,
    RUNTIME_ERROR_CODES,
    SCHEMA_VERSION,
    SUPPORTED_OUTPUTS,
    TIMESTEPS,
    TOOL_ID,
    TOOL_VERSION,
)


class ContractError(RuntimeError):
    def __init__(
        self,
        code="INVALID_RUNTIME_REQUEST",  # type: str
        http_status=422,  # type: int
        safe_message="Runtime request is invalid.",  # type: str
        retryable=False,  # type: bool
        failed_step=None,  # type: Optional[str]
        details=None,  # type: Optional[Mapping[str, Any]]
    ):
        # type: (...) -> None
        super().__init__(safe_message)
        self.code = code
        self.http_status = http_status
        self.safe_message = safe_message
        self.retryable = retryable
        self.failed_step = failed_step
        self.details = dict(details or {})


@dataclass(frozen=True)
class ExecuteRequest:
    request_id: str
    task_id: str
    tool_run_id: str
    process_parameters: Dict[str, Any]
    requested_outputs: Tuple[str, ...]
    runtime_parameters: Dict[str, Any]


_REQUEST_FIELDS = frozenset(
    (
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
    )
)
_RUNTIME_PARAMETER_FIELDS = frozenset(
    ("seed", "num_samples", "guide_scale", "timesteps")
)
_IMAGE_FIELDS = frozenset(
    (
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
    )
)
_DIAGNOSTIC_FIELDS = frozenset(
    (
        "step",
        "status",
        "started_at",
        "completed_at",
        "duration_ms",
        "error_code",
        "safe_error_message",
    )
)
_WARNING_FIELDS = frozenset(("code", "safe_message"))
_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}\Z")
_UTC_MILLISECOND_PATTERN = re.compile(
    r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z\Z"
)


def _invalid(
    http_status=422,  # type: int
):
    # type: (...) -> ContractError
    return ContractError(http_status=http_status)


def _reject_json_constant(_value):
    # type: (str) -> None
    raise ValueError("JSON constants are not allowed.")


def _finite_decimal(value):
    # type: (Any) -> Optional[Decimal]
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        converted = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    if not converted.is_finite():
        return None
    return converted


def _validate_id(value):
    # type: (Any) -> str
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > MAX_ID_CHARS
        or not value.isprintable()
    ):
        raise _invalid()
    return value


def _validate_versions(payload):
    # type: (Mapping[str, Any]) -> None
    if payload.get("runtime_contract_version") != RUNTIME_CONTRACT_VERSION:
        raise ContractError(
            code="SCHEMA_VERSION_MISMATCH",
            http_status=409,
            safe_message="Runtime contract version is incompatible.",
        )
    if payload.get("tool_id") != TOOL_ID:
        raise ContractError(
            code="UNSUPPORTED_TOOL",
            http_status=422,
            safe_message="Runtime does not support this tool.",
        )
    if payload.get("tool_version") != TOOL_VERSION:
        raise ContractError(
            code="TOOL_VERSION_MISMATCH",
            http_status=409,
            safe_message="Tool version is incompatible.",
        )
    if payload.get("schema_version") != SCHEMA_VERSION:
        raise ContractError(
            code="SCHEMA_VERSION_MISMATCH",
            http_status=409,
            safe_message="Schema version is incompatible.",
        )


def _validate_process_parameters(value):
    # type: (Any) -> Dict[str, Any]
    if not isinstance(value, dict) or set(value) != set(
        PROCESS_PARAMETER_ORDER
    ):
        raise _invalid()
    normalized = {}  # type: Dict[str, Any]
    for field_name in PROCESS_PARAMETER_ORDER:
        raw_value = value.get(field_name)
        lower, upper, integer_required = PROCESS_PARAMETER_RANGES[field_name]
        converted = _finite_decimal(raw_value)
        if (
            converted is None
            or converted < Decimal(str(lower))
            or converted > Decimal(str(upper))
        ):
            raise _invalid()
        if integer_required:
            if (
                not isinstance(raw_value, int)
                or isinstance(raw_value, bool)
                or converted != converted.to_integral_value()
            ):
                raise _invalid()
        elif converted * 10 != (converted * 10).to_integral_value():
            raise _invalid()
        normalized[field_name] = raw_value
    return normalized


def _validate_requested_outputs(value):
    # type: (Any) -> Tuple[str, ...]
    if (
        not isinstance(value, list)
        or not value
        or len(value) > len(SUPPORTED_OUTPUTS)
        or any(not isinstance(item, str) for item in value)
        or len(set(value)) != len(value)
        or not set(value).issubset(SUPPORTED_OUTPUTS)
    ):
        raise _invalid()
    return tuple(value)


def _validate_runtime_parameters(value):
    # type: (Any) -> Dict[str, Any]
    if not isinstance(value, dict) or set(value) != _RUNTIME_PARAMETER_FIELDS:
        raise _invalid()
    seed = value.get("seed")
    if (
        isinstance(seed, bool)
        or not isinstance(seed, int)
        or not 0 <= seed < 2**63
    ):
        raise _invalid()
    if value.get("num_samples") != NUM_SAMPLES or isinstance(
        value.get("num_samples"), bool
    ):
        raise _invalid()
    if _finite_decimal(value.get("guide_scale")) != Decimal(
        str(GUIDE_SCALE)
    ):
        raise _invalid()
    if value.get("timesteps") != TIMESTEPS or isinstance(
        value.get("timesteps"), bool
    ):
        raise _invalid()
    return {
        "seed": seed,
        "num_samples": NUM_SAMPLES,
        "guide_scale": GUIDE_SCALE,
        "timesteps": TIMESTEPS,
    }


def parse_execute_request(
    body,  # type: bytes
    content_type="application/json",  # type: str
):
    # type: (...) -> ExecuteRequest
    media_type = content_type.split(";", 1)[0].strip().lower()
    if media_type != "application/json":
        raise _invalid(http_status=415)
    if not isinstance(body, bytes) or len(body) > MAX_REQUEST_BYTES:
        raise _invalid(http_status=413)
    try:
        decoded = json.loads(
            body.decode("utf-8"),
            parse_constant=_reject_json_constant,
        )
    except (
        UnicodeDecodeError,
        ValueError,
        json.JSONDecodeError,
        RecursionError,
    ):
        raise _invalid(http_status=400) from None
    if not isinstance(decoded, dict) or set(decoded) != _REQUEST_FIELDS:
        raise _invalid()
    _validate_versions(decoded)
    return ExecuteRequest(
        request_id=_validate_id(decoded.get("request_id")),
        task_id=_validate_id(decoded.get("task_id")),
        tool_run_id=_validate_id(decoded.get("tool_run_id")),
        process_parameters=_validate_process_parameters(
            decoded.get("process_parameters")
        ),
        requested_outputs=_validate_requested_outputs(
            decoded.get("requested_outputs")
        ),
        runtime_parameters=_validate_runtime_parameters(
            decoded.get("runtime_parameters")
        ),
    )


def _safe_details(value):
    # type: (Optional[Mapping[str, Any]]) -> Dict[str, Any]
    if value is None:
        return {}
    if not isinstance(value, Mapping) or len(value) > MAX_SAFE_DETAILS:
        raise ValueError("Runtime error details are invalid.")
    result = {}  # type: Dict[str, Any]
    for key, item in value.items():
        if (
            not isinstance(key, str)
            or not key
            or len(key) > MAX_SAFE_DETAIL_KEY_CHARS
            or not key.isprintable()
        ):
            raise ValueError("Runtime error details are invalid.")
        if item is None or isinstance(item, bool) or isinstance(item, int):
            result[key] = item
        elif isinstance(item, float) and math.isfinite(item):
            result[key] = item
        elif (
            isinstance(item, str)
            and item
            and len(item) <= MAX_SAFE_MESSAGE_CHARS
            and item.isprintable()
        ):
            result[key] = item
        else:
            raise ValueError("Runtime error details are invalid.")
    return result


def safe_error(
    code,  # type: str
    safe_message,  # type: str
    retryable=False,  # type: bool
    failed_step=None,  # type: Optional[str]
    details=None,  # type: Optional[Mapping[str, Any]]
):
    # type: (...) -> Dict[str, Any]
    if (
        code not in RUNTIME_ERROR_CODES
        or not isinstance(safe_message, str)
        or not safe_message.strip()
        or len(safe_message) > MAX_SAFE_MESSAGE_CHARS
        or not safe_message.isprintable()
        or not isinstance(retryable, bool)
        or (code == "RUNTIME_BUSY" and retryable is not True)
        or (
            failed_step is not None
            and failed_step not in DIAGNOSTIC_STEPS
        )
    ):
        raise ValueError("Runtime error is invalid.")
    return {
        "runtime_contract_version": RUNTIME_CONTRACT_VERSION,
        "error": {
            "code": code,
            "safe_message": safe_message,
            "retryable": retryable,
            "failed_step": failed_step,
            "details": _safe_details(details),
        },
    }


def _validate_bounded_records(
    values,  # type: Any
    maximum,  # type: int
):
    # type: (...) -> Any
    if (
        not isinstance(values, (list, tuple))
        or len(values) > maximum
        or any(not isinstance(item, dict) for item in values)
    ):
        raise ContractError(
            code="INTERNAL_RUNTIME_ERROR",
            http_status=500,
            safe_message="Runtime response is invalid.",
        )
    return [dict(item) for item in values]


def _invalid_response():
    # type: () -> ContractError
    return ContractError(
        code="INTERNAL_RUNTIME_ERROR",
        http_status=500,
        safe_message="Runtime response is invalid.",
    )


def _validate_warning_records(values):
    # type: (Any) -> Any
    records = _validate_bounded_records(values, MAX_WARNINGS)
    for record in records:
        code = record.get("code")
        message = record.get("safe_message")
        if (
            set(record) != _WARNING_FIELDS
            or not isinstance(code, str)
            or not code
            or len(code) > 64
            or not code.isprintable()
            or not isinstance(message, str)
            or not message.strip()
            or len(message) > MAX_SAFE_MESSAGE_CHARS
            or not message.isprintable()
        ):
            raise _invalid_response()
    return records


def _validate_diagnostic_records(values):
    # type: (Any) -> Any
    records = _validate_bounded_records(values, MAX_DIAGNOSTICS)
    seen_steps = set()
    for record in records:
        step = record.get("step")
        status = record.get("status")
        started_at = record.get("started_at")
        completed_at = record.get("completed_at")
        duration_ms = record.get("duration_ms")
        error_code = record.get("error_code")
        message = record.get("safe_error_message")
        if (
            set(record) != _DIAGNOSTIC_FIELDS
            or step not in DIAGNOSTIC_STEPS
            or step in seen_steps
            or status not in ("SUCCEEDED", "FAILED")
            or not isinstance(started_at, str)
            or _UTC_MILLISECOND_PATTERN.fullmatch(started_at) is None
            or not isinstance(completed_at, str)
            or _UTC_MILLISECOND_PATTERN.fullmatch(completed_at) is None
            or isinstance(duration_ms, bool)
            or not isinstance(duration_ms, (int, float))
            or not math.isfinite(duration_ms)
            or duration_ms < 0
        ):
            raise _invalid_response()
        if status == "SUCCEEDED":
            if error_code is not None or message is not None:
                raise _invalid_response()
        elif (
            error_code not in RUNTIME_ERROR_CODES
            or not isinstance(message, str)
            or not message.strip()
            or len(message) > MAX_SAFE_MESSAGE_CHARS
            or not message.isprintable()
        ):
            raise _invalid_response()
        seen_steps.add(step)
    return records


def _validate_image_records(values):
    # type: (Any) -> Any
    records = _validate_bounded_records(values, 1)
    for record in records:
        encoded = record.get("data_base64")
        digest = record.get("sha256")
        if (
            set(record) != _IMAGE_FIELDS
            or record.get("image_role")
            not in ("generated_sem", "intermediate_sem")
            or not isinstance(record.get("requested_output"), bool)
            or record.get("dtype") != "float32"
            or record.get("numpy_dtype") != "<f4"
            or record.get("shape") != [512, 512]
            or record.get("channel_layout") != "GRAYSCALE_2D"
            or record.get("value_range") != [-1.0, 1.0]
            or record.get("encoding") != "base64+npy"
            or record.get("byte_order") != "little"
            or record.get("array_order") != "C"
            or not isinstance(digest, str)
            or _SHA256_PATTERN.fullmatch(digest) is None
            or not isinstance(encoded, str)
            or not encoded
        ):
            raise _invalid_response()
        try:
            payload = base64.b64decode(encoded, validate=True)
        except (ValueError, binascii.Error):
            raise _invalid_response() from None
        if not payload or sha256(payload).hexdigest() != digest:
            raise _invalid_response()
    return records


def _validate_result_error(value):
    # type: (Any) -> Any
    if not isinstance(value, dict):
        raise _invalid_response()
    required = frozenset(
        ("code", "safe_message", "retryable", "failed_step", "details")
    )
    if set(value) != required:
        raise _invalid_response()
    try:
        normalized = safe_error(
            code=value.get("code"),
            safe_message=value.get("safe_message"),
            retryable=value.get("retryable"),
            failed_step=value.get("failed_step"),
            details=value.get("details"),
        )["error"]
    except ValueError:
        raise _invalid_response() from None
    if normalized != value:
        raise _invalid_response()
    return normalized


def _validate_performance_response_data(data, completed):
    # type: (Any, Tuple[str, ...]) -> Dict[str, Any]
    if not isinstance(data, dict):
        raise _invalid_response()
    if "mechanical_properties" not in completed:
        if data:
            raise _invalid_response()
        return {}
    if set(data) != {"yield_strength", "elongation"}:
        raise _invalid_response()
    normalized = {}  # type: Dict[str, Any]
    for field_name, unit in (
        ("yield_strength", "MPa"),
        ("elongation", "%"),
    ):
        item = data.get(field_name)
        value = item.get("value") if isinstance(item, dict) else None
        if (
            not isinstance(item, dict)
            or set(item) != {"value", "unit"}
            or isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or item.get("unit") != unit
        ):
            raise _invalid_response()
        normalized[field_name] = dict(item)
    return normalized


def build_execute_response(request, result):
    # type: (ExecuteRequest, Any) -> Dict[str, Any]
    try:
        status = result.status
        completed = tuple(result.completed_outputs)
        failed = tuple(result.failed_outputs)
        raw_error = result.error
    except (AttributeError, TypeError):
        raise _invalid_response() from None
    requested = request.requested_outputs
    if (
        status not in ("SUCCEEDED", "PARTIALLY_SUCCEEDED", "FAILED")
        or any(not isinstance(item, str) for item in completed + failed)
        or len(set(completed)) != len(completed)
        or len(set(failed)) != len(failed)
        or set(completed) & set(failed)
        or set(completed) | set(failed) != set(requested)
        or any(item not in requested for item in completed + failed)
        or (
            status == "SUCCEEDED"
            and (completed != requested or failed or raw_error is not None)
        )
        or (
            status == "PARTIALLY_SUCCEEDED"
            and (not completed or not failed or raw_error is None)
        )
        or (
            status == "FAILED"
            and (completed or not failed or raw_error is None)
        )
    ):
        raise _invalid_response()
    warnings = _validate_warning_records(result.warnings)
    diagnostics = _validate_diagnostic_records(result.diagnostics)
    images = _validate_image_records(result.images)
    data = _validate_performance_response_data(result.data, completed)
    if status in ("SUCCEEDED", "PARTIALLY_SUCCEEDED") and len(images) != 1:
        raise _invalid_response()
    if len(images) == 1:
        image = images[0]
        if "sem_image" in completed:
            expected_image = ("generated_sem", True)
        elif requested == ("mechanical_properties",):
            expected_image = ("intermediate_sem", False)
        else:
            raise _invalid_response()
        if (
            image["image_role"],
            image["requested_output"],
        ) != expected_image:
            raise _invalid_response()
    model_bundle_id = result.model_bundle_id
    if model_bundle_id != MODEL_BUNDLE_ID:
        raise _invalid_response()
    error = (
        None
        if raw_error is None
        else _validate_result_error(raw_error)
    )
    payload = {
        "runtime_contract_version": RUNTIME_CONTRACT_VERSION,
        "request_id": request.request_id,
        "task_id": request.task_id,
        "tool_run_id": request.tool_run_id,
        "tool_id": TOOL_ID,
        "tool_version": TOOL_VERSION,
        "schema_version": SCHEMA_VERSION,
        "status": status,
        "requested_outputs": list(request.requested_outputs),
        "completed_outputs": list(completed),
        "failed_outputs": list(failed),
        "data": data,
        "images": images,
        "warnings": warnings,
        "diagnostics": diagnostics,
        "actual_runtime_parameters": dict(request.runtime_parameters),
        "model_bundle_id": model_bundle_id,
        "error": error,
    }
    serialize_response(payload)
    return payload


def serialize_response(payload):
    # type: (Mapping[str, Any]) -> bytes
    try:
        encoded = json.dumps(
            payload,
            ensure_ascii=True,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError):
        raise ContractError(
            code="INTERNAL_RUNTIME_ERROR",
            http_status=500,
            safe_message="Runtime response is invalid.",
        ) from None
    if len(encoded) > MAX_RESPONSE_BYTES:
        raise ContractError(
            code="INTERNAL_RUNTIME_ERROR",
            http_status=500,
            safe_message="Runtime response is too large.",
        )
    return encoded
