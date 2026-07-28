from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import math
import re
from types import MappingProxyType
from typing import Final


PENDING: Final = "PENDING"
RUNNING: Final = "RUNNING"
SUCCEEDED: Final = "SUCCEEDED"
FAILED: Final = "FAILED"
CHAT_ORCHESTRATION: Final = "CHAT_ORCHESTRATION"
TOOL_RESULT_EXPLANATION: Final = "TOOL_RESULT_EXPLANATION"

STATUSES: Final = frozenset({PENDING, RUNNING, SUCCEEDED, FAILED})
PURPOSES: Final = frozenset(
    {CHAT_ORCHESTRATION, TOOL_RESULT_EXPLANATION}
)
SHA256_PATTERN: Final = re.compile(r"[0-9a-f]{64}\Z")
PROVIDER_REQUEST_ID_PATTERN: Final = re.compile(
    r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}\Z"
)
MAX_JSON_BYTES: Final = 4096
MAX_SAFE_ERROR_MESSAGE_CHARS: Final = 256
ZTA35G_TOOL_ID: Final = "zta35g_sem_virtual_lab"
ZTA35G_INPUT_FIELDS: Final = frozenset(
    {
        "material",
        "solution_temperature",
        "solution_time",
        "aging_temperature",
        "aging_time",
    }
)


class _FrozenJSONList(tuple[object, ...]):
    pass


def _require_non_blank(value: str, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be non-blank text.")


def _require_optional_text(value: str | None, field_name: str) -> None:
    if value is not None:
        _require_non_blank(value, field_name)


def _require_safe_error_message(value: str | None) -> None:
    if value is None:
        return
    _require_non_blank(value, "safe_error_message")
    if len(value) > MAX_SAFE_ERROR_MESSAGE_CHARS or not value.isprintable():
        raise ValueError(
            "safe_error_message must be bounded printable single-line text."
        )


def _require_utc(value: datetime, field_name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware UTC.")
    if value.utcoffset() != timezone.utc.utcoffset(value):
        raise ValueError(f"{field_name} must be timezone-aware UTC.")


def _freeze_json(value: object) -> object:
    if isinstance(value, Mapping):
        return MappingProxyType(
            {key: _freeze_json(item) for key, item in value.items()}
        )
    if isinstance(value, (list, _FrozenJSONList)):
        return _FrozenJSONList(_freeze_json(item) for item in value)
    return value


def _plain_json(value: object, field_name: str) -> object:
    if isinstance(value, Mapping):
        result: dict[str, object] = {}
        for key, item in value.items():
            if type(key) is not str:
                raise ValueError(f"{field_name} must use built-in text keys.")
            result[key] = _plain_json(item, field_name)
        return result
    if type(value) in (list, tuple, _FrozenJSONList):
        return [_plain_json(item, field_name) for item in value]
    if value is None or type(value) in (str, bool, int):
        return value
    if type(value) is float and math.isfinite(value):
        return value
    raise ValueError(f"{field_name} must contain built-in safe JSON values.")


def _bounded_frozen_object(
    value: Mapping[str, object],
    field_name: str,
) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{field_name} must be a JSON object.")
    plain_value = _plain_json(value, field_name)
    try:
        encoded = json.dumps(
            plain_value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError):
        raise ValueError(f"{field_name} must contain safe JSON values.") from None
    if len(encoded) > MAX_JSON_BYTES:
        raise ValueError(f"{field_name} exceeds the safe size limit.")
    return _freeze_json(plain_value)  # type: ignore[return-value]


def _require_exact_keys(
    value: Mapping[str, object],
    expected: set[str],
    field_name: str,
) -> None:
    if set(value) != expected:
        raise ValueError(f"{field_name} does not match its controlled schema.")


def _controlled_generation_parameters(
    value: Mapping[str, object],
    purpose: str,
) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError("generation_parameters must be a JSON object.")
    legacy_keys = {"temperature", "max_tokens"}
    deepseek_keys = {
        "temperature",
        "max_tokens",
        "thinking_mode",
        "response_format",
        "streaming",
    }
    if set(value) not in {frozenset(legacy_keys), frozenset(deepseek_keys)}:
        raise ValueError(
            "generation_parameters does not match its controlled schema."
        )
    temperature = value["temperature"]
    if (
        type(temperature) not in (int, float)
        or not math.isfinite(float(temperature))
        or temperature < 0
    ):
        raise ValueError(
            "generation_parameters.temperature must be a finite "
            "nonnegative number."
        )
    max_tokens = value["max_tokens"]
    if (
        type(max_tokens) is not int
        or max_tokens <= 0
    ):
        raise ValueError(
            "generation_parameters.max_tokens must be a positive integer."
        )
    if set(value) == deepseek_keys:
        response_format = value["response_format"]
        expected_response_format, expected_max_tokens = {
            CHAT_ORCHESTRATION: ("json_object", 1024),
            TOOL_RESULT_EXPLANATION: ("text", 768),
        }[purpose]
        if (
            temperature != 0
            or value["thinking_mode"] != "disabled"
            or type(value["streaming"]) is not bool
            or value["streaming"] is not False
            or response_format != expected_response_format
            or max_tokens != expected_max_tokens
        ):
            raise ValueError(
                "generation_parameters does not match its controlled schema."
            )
    return _bounded_frozen_object(value, "generation_parameters")


def _controlled_usage(
    value: Mapping[str, object] | None,
) -> Mapping[str, object] | None:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise ValueError("usage must be a JSON object.")
    _require_exact_keys(value, {"input_tokens", "output_tokens"}, "usage")
    for field_name in ("input_tokens", "output_tokens"):
        token_count = value[field_name]
        if (
            type(token_count) is not int
            or token_count < 0
        ):
            raise ValueError(f"usage.{field_name} must be a nonnegative integer.")
    return _bounded_frozen_object(value, "usage")


def _require_controlled_field_list(value: object, field_name: str) -> None:
    if type(value) not in (list, tuple, _FrozenJSONList) or not all(
        type(item) is str and item in ZTA35G_INPUT_FIELDS
        for item in value
    ):
        raise ValueError(
            f"structured_output_summary.{field_name} must contain "
            "controlled field names."
        )


def _controlled_structured_output_summary(
    value: Mapping[str, object] | None,
    purpose: str,
) -> Mapping[str, object] | None:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise ValueError("structured_output_summary must be a JSON object.")
    if purpose != CHAT_ORCHESTRATION:
        raise ValueError(
            "structured_output_summary is only allowed for CHAT_ORCHESTRATION."
        )

    route = value.get("route")
    if type(route) is not str:
        raise ValueError("structured_output_summary.route must be built-in text.")
    if route == "KNOWLEDGE_ANSWER":
        keys = set(value)
        allowed_shapes = (
            {"route"},
            {"route", "answer_length", "answer_digest"},
        )
        if keys not in allowed_shapes:
            raise ValueError(
                "structured_output_summary does not match its controlled schema."
            )
        if "answer_length" in value:
            answer_length = value["answer_length"]
            answer_digest = value["answer_digest"]
            if (
                isinstance(answer_length, bool)
                or not isinstance(answer_length, int)
                or answer_length < 0
                or type(answer_digest) is not str
                or SHA256_PATTERN.fullmatch(answer_digest) is None
            ):
                raise ValueError(
                    "structured_output_summary contains an invalid answer digest."
                )
    elif route == "TOOL_EXECUTION":
        _require_exact_keys(
            value,
            {"route", "tool_id"},
            "structured_output_summary",
        )
        if value["tool_id"] != ZTA35G_TOOL_ID:
            raise ValueError(
                "structured_output_summary contains an unsupported tool_id."
            )
    elif route == "NEEDS_INPUT":
        _require_exact_keys(
            value,
            {
                "route",
                "tool_id",
                "missing_fields",
                "ambiguous_fields",
            },
            "structured_output_summary",
        )
        if value["tool_id"] != ZTA35G_TOOL_ID:
            raise ValueError(
                "structured_output_summary contains an unsupported tool_id."
            )
        _require_controlled_field_list(value["missing_fields"], "missing_fields")
        _require_controlled_field_list(
            value["ambiguous_fields"],
            "ambiguous_fields",
        )
    else:
        raise ValueError("structured_output_summary contains an unknown route.")
    return _bounded_frozen_object(value, "structured_output_summary")


@dataclass(frozen=True, slots=True)
class LLMCall:
    llm_call_id: str
    task_id: str
    conversation_id: str
    request_id: str
    purpose: str
    input_result_id: str | None
    provider: str
    model_name: str
    prompt_template_id: str | None
    prompt_template_version: str | None
    prompt_digest: str | None
    generation_parameters: Mapping[str, object]
    structured_output_summary: Mapping[str, object] | None
    usage: Mapping[str, object] | None
    provider_request_id: str | None
    status: str
    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None
    duration_ms: int | None
    error_code: str | None
    safe_error_message: str | None

    def __post_init__(self) -> None:
        for field_name in (
            "llm_call_id",
            "task_id",
            "conversation_id",
            "request_id",
            "provider",
            "model_name",
        ):
            _require_non_blank(getattr(self, field_name), field_name)
        for field_name in (
            "input_result_id",
            "prompt_template_id",
            "prompt_template_version",
            "error_code",
        ):
            _require_optional_text(getattr(self, field_name), field_name)
        if (
            self.provider_request_id is not None
            and PROVIDER_REQUEST_ID_PATTERN.fullmatch(
                self.provider_request_id
            )
            is None
        ):
            raise ValueError(
                "provider_request_id must be a bounded controlled identifier."
            )
        _require_safe_error_message(self.safe_error_message)
        if self.purpose not in PURPOSES:
            raise ValueError("purpose is not allowed.")
        if self.purpose == CHAT_ORCHESTRATION and self.input_result_id is not None:
            raise ValueError(
                "input_result_id must be null for CHAT_ORCHESTRATION."
            )
        if self.purpose == TOOL_RESULT_EXPLANATION and self.input_result_id is None:
            raise ValueError(
                "input_result_id is required for TOOL_RESULT_EXPLANATION."
            )
        if self.prompt_digest is not None and not SHA256_PATTERN.fullmatch(
            self.prompt_digest
        ):
            raise ValueError("prompt_digest must be lowercase SHA-256 hex.")
        if self.status not in STATUSES:
            raise ValueError("status is not allowed.")
        if self.duration_ms is not None and (
            not isinstance(self.duration_ms, int)
            or isinstance(self.duration_ms, bool)
            or self.duration_ms < 0
        ):
            raise ValueError("duration_ms must be a nonnegative integer or null.")

        _require_utc(self.created_at, "created_at")
        if self.started_at is not None:
            _require_utc(self.started_at, "started_at")
            if self.started_at < self.created_at:
                raise ValueError("started_at must not be earlier than created_at.")
        if self.completed_at is not None:
            _require_utc(self.completed_at, "completed_at")
            if self.started_at is None or self.completed_at < self.started_at:
                raise ValueError("completed_at must not be earlier than started_at.")

        if self.status == PENDING and any(
            value is not None
            for value in (
                self.started_at,
                self.completed_at,
                self.duration_ms,
                self.error_code,
                self.safe_error_message,
            )
        ):
            raise ValueError("PENDING cannot contain execution or error fields.")
        if self.status == RUNNING and (
            self.started_at is None
            or any(
                value is not None
                for value in (
                    self.completed_at,
                    self.duration_ms,
                    self.error_code,
                    self.safe_error_message,
                )
            )
        ):
            raise ValueError("RUNNING requires only started_at.")
        if self.status in {SUCCEEDED, FAILED} and (
            self.started_at is None
            or self.completed_at is None
            or self.duration_ms is None
        ):
            raise ValueError(f"{self.status} requires terminal timing fields.")
        if self.status == SUCCEEDED and (
            self.error_code is not None or self.safe_error_message is not None
        ):
            raise ValueError("SUCCEEDED cannot retain stale errors.")
        if self.status == FAILED and self.error_code is None:
            raise ValueError("FAILED requires error_code.")

        object.__setattr__(
            self,
            "generation_parameters",
            _controlled_generation_parameters(
                self.generation_parameters,
                self.purpose,
            ),
        )
        object.__setattr__(
            self,
            "structured_output_summary",
            _controlled_structured_output_summary(
                self.structured_output_summary,
                self.purpose,
            ),
        )
        object.__setattr__(
            self,
            "usage",
            _controlled_usage(self.usage),
        )
