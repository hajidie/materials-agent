from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import math
import re
from types import MappingProxyType
from typing import Final, Mapping


SUCCEEDED: Final = "SUCCEEDED"
PARTIALLY_SUCCEEDED: Final = "PARTIALLY_SUCCEEDED"
FAILED: Final = "FAILED"
TOOL_RESULT_STATUSES: Final = frozenset({SUCCEEDED, PARTIALLY_SUCCEEDED, FAILED})
SUPPORTED_OUTPUTS: Final = frozenset({"sem_image", "mechanical_properties"})
MAX_SAFE_JSON_BYTES: Final = 4096
MAX_SAFE_JSON_DEPTH: Final = 4
MAX_SAFE_JSON_ARRAY_ITEMS: Final = 64
MAX_SAFE_JSON_OBJECT_ITEMS: Final = 64
MAX_SAFE_JSON_STRING_CHARS: Final = 1024
SHA256_PATTERN: Final = re.compile(r"[0-9a-f]{64}\Z")
_FORBIDDEN_JSON_KEY_PARTS: Final = frozenset(
    {
        "api_key",
        "adapter",
        "bucket",
        "compatibility",
        "endpoint",
        "fingerprint",
        "internal",
        "model_bundle",
        "model_path",
        "object_key",
        "password",
        "path",
        "prompt",
        "raw",
        "response_body",
        "secret",
        "tensor",
        "token",
        "traceback",
        "weight",
    }
)


def _require_text(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be non-blank text.")
    return value


def _require_utc(value: object, field_name: str) -> datetime:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
        or value.utcoffset() != timezone.utc.utcoffset(value)
    ):
        raise ValueError(f"{field_name} must be timezone-aware UTC.")
    return value


def _safe_json_key(key: str, field_name: str) -> None:
    normalized = key.lower().replace("-", "_")
    if any(part in normalized for part in _FORBIDDEN_JSON_KEY_PARTS):
        raise ValueError(f"{field_name} contains forbidden internal data.")


def _freeze_safe_json(value: object, field_name: str, depth: int = 0) -> object:
    if depth > MAX_SAFE_JSON_DEPTH:
        raise ValueError(f"{field_name} exceeds the safe JSON depth limit.")
    value_type = type(value)
    if value_type is type(None) or value_type is bool or value_type is int:
        return value
    if value_type is float:
        if not math.isfinite(value):
            raise ValueError(f"{field_name} must contain finite JSON numbers.")
        return value
    if value_type is str:
        if len(value) > MAX_SAFE_JSON_STRING_CHARS or not value.isprintable():
            raise ValueError(f"{field_name} contains unbounded or unsafe text.")
        return value
    if value_type is list:
        if len(value) > MAX_SAFE_JSON_ARRAY_ITEMS:
            raise ValueError(f"{field_name} exceeds the safe JSON array limit.")
        return tuple(_freeze_safe_json(item, field_name, depth + 1) for item in value)
    if value_type is dict:
        if len(value) > MAX_SAFE_JSON_OBJECT_ITEMS:
            raise ValueError(f"{field_name} exceeds the safe JSON object limit.")
        frozen: dict[str, object] = {}
        for key, item in value.items():
            if type(key) is not str:
                raise ValueError(f"{field_name} must use text JSON keys.")
            _safe_json_key(key, field_name)
            frozen[key] = _freeze_safe_json(item, field_name, depth + 1)
        return MappingProxyType(frozen)
    raise ValueError(f"{field_name} must contain safe JSON values.")


def _safe_json_size(value: object, field_name: str) -> None:
    def plain_json(item: object) -> object:
        if isinstance(item, Mapping):
            return {
                key: plain_json(entry)
                for key, entry in item.items()
            }
        if isinstance(item, tuple):
            return [plain_json(entry) for entry in item]
        return item

    try:
        encoded = json.dumps(
            plain_json(value),
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError):
        raise ValueError(
            f"{field_name} must contain safe JSON values."
        ) from None
    if len(encoded) > MAX_SAFE_JSON_BYTES:
        raise ValueError(f"{field_name} exceeds the safe size limit.")


def _freeze_json_object(value: object, field_name: str) -> Mapping[str, object]:
    if type(value) is not dict:
        raise ValueError(f"{field_name} must be a JSON object.")
    frozen = _freeze_safe_json(value, field_name)
    assert isinstance(frozen, Mapping)
    _safe_json_size(frozen, field_name)
    return frozen


def _freeze_json_array(value: object, field_name: str) -> tuple[object, ...]:
    if type(value) is not list:
        raise ValueError(f"{field_name} must be a JSON array.")
    frozen = _freeze_safe_json(value, field_name)
    assert isinstance(frozen, tuple)
    _safe_json_size(frozen, field_name)
    return frozen


def _normalize_requested_outputs(value: object) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        raise ValueError("requested_outputs must be a text array.")
    normalized: list[str] = []
    for output in value:
        if type(output) is not str or output not in SUPPORTED_OUTPUTS:
            raise ValueError("requested_outputs supports only registered outputs.")
        if output not in normalized:
            normalized.append(output)
    if not normalized:
        raise ValueError("requested_outputs must be nonempty.")
    return tuple(normalized)


def _normalize_output_subset(
    value: object,
    field_name: str,
    requested_outputs: tuple[str, ...],
) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        raise ValueError(f"{field_name} must be a text array.")
    values: set[str] = set()
    for output in value:
        if type(output) is not str or output not in requested_outputs:
            raise ValueError(f"{field_name} must be a requested output subset.")
        values.add(output)
    return tuple(output for output in requested_outputs if output in values)


def _validate_data(
    data: Mapping[str, object],
    completed_outputs: tuple[str, ...],
) -> None:
    if "mechanical_properties" not in completed_outputs:
        if data:
            raise ValueError("data must be empty without mechanical_properties.")
        return
    if set(data) != {"yield_strength", "elongation"}:
        raise ValueError("data must contain only controlled mechanical properties.")
    for name, unit in (("yield_strength", "MPa"), ("elongation", "%")):
        property_value = data[name]
        if not isinstance(property_value, Mapping) or set(property_value) != {"value", "unit"}:
            raise ValueError("data must use the controlled mechanical property schema.")
        value = property_value["value"]
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or property_value["unit"] != unit
        ):
            raise ValueError("data must contain finite mechanical values in fixed units.")


def _validate_provenance(value: Mapping[str, object]) -> None:
    if set(value) != {
        "input_revision",
        "normalized_process_parameters",
        "actual_runtime_parameters",
    }:
        raise ValueError("provenance must use the controlled public schema.")
    input_revision = value["input_revision"]
    if (
        type(input_revision) is not int
        or input_revision <= 0
    ):
        raise ValueError("provenance.input_revision must be positive.")
    if not isinstance(
        value["normalized_process_parameters"],
        Mapping,
    ) or not isinstance(
        value["actual_runtime_parameters"],
        Mapping,
    ):
        raise ValueError("provenance must contain controlled JSON objects.")


def _validate_error(
    value: Mapping[str, object] | None,
    status: str,
) -> None:
    if status == SUCCEEDED:
        if value is not None:
            raise ValueError("SUCCEEDED result cannot contain an error.")
        return
    if value is None or set(value) != {
        "code",
        "safe_message",
        "retryable",
    }:
        raise ValueError("Non-success result requires a controlled error.")
    _require_text(value["code"], "error")
    safe_message = _require_text(value["safe_message"], "error")
    if len(safe_message) > 256 or not safe_message.isprintable():
        raise ValueError("error contains unsafe text.")
    if type(value["retryable"]) is not bool:
        raise ValueError("error.retryable must be boolean.")


@dataclass(frozen=True, slots=True)
class ToolResult:
    result_id: str
    task_id: str
    tool_run_id: str
    actor_id: str
    status: str
    requested_outputs: tuple[str, ...] | list[str]
    completed_outputs: tuple[str, ...] | list[str]
    failed_outputs: tuple[str, ...] | list[str]
    data: Mapping[str, object] | dict[str, object]
    warnings: tuple[object, ...] | list[object]
    provenance: Mapping[str, object] | dict[str, object]
    error: Mapping[str, object] | dict[str, object] | None
    tool_id: str
    tool_version: str
    schema_hash: str
    created_at: datetime

    def __post_init__(self) -> None:
        for field_name in (
            "result_id",
            "task_id",
            "tool_run_id",
            "actor_id",
            "tool_id",
            "tool_version",
        ):
            _require_text(getattr(self, field_name), field_name)
        if SHA256_PATTERN.fullmatch(self.schema_hash) is None:
            raise ValueError("schema_hash must be lowercase SHA-256 hex.")
        if self.status not in TOOL_RESULT_STATUSES:
            raise ValueError("status is not an allowed ToolResult status.")
        requested_outputs = _normalize_requested_outputs(self.requested_outputs)
        completed_outputs = _normalize_output_subset(
            self.completed_outputs,
            "completed_outputs",
            requested_outputs,
        )
        failed_outputs = _normalize_output_subset(
            self.failed_outputs,
            "failed_outputs",
            requested_outputs,
        )
        if set(completed_outputs) & set(failed_outputs):
            raise ValueError("completed_outputs and failed_outputs must be disjoint.")
        if set(completed_outputs) | set(failed_outputs) != set(requested_outputs):
            raise ValueError("terminal ToolResult outputs must cover all requested outputs.")
        expected_status = (
            SUCCEEDED
            if len(completed_outputs) == len(requested_outputs)
            else PARTIALLY_SUCCEEDED
            if completed_outputs
            else FAILED
        )
        if self.status != expected_status:
            raise ValueError("status must match completed and failed outputs.")
        data = _freeze_json_object(self.data, "data")
        warnings = _freeze_json_array(self.warnings, "warnings")
        provenance = _freeze_json_object(self.provenance, "provenance")
        error = None if self.error is None else _freeze_json_object(self.error, "error")
        _validate_data(data, completed_outputs)
        _validate_provenance(provenance)
        _validate_error(error, self.status)
        _require_utc(self.created_at, "created_at")
        object.__setattr__(self, "requested_outputs", requested_outputs)
        object.__setattr__(self, "completed_outputs", completed_outputs)
        object.__setattr__(self, "failed_outputs", failed_outputs)
        object.__setattr__(self, "data", data)
        object.__setattr__(self, "warnings", warnings)
        object.__setattr__(self, "provenance", provenance)
        object.__setattr__(self, "error", error)
