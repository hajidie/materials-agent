from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
import json
import math
import re
from types import MappingProxyType
from typing import Protocol

from materialsagent.domain.ports.tool_registry import ToolRef
from materialsagent.domain.ports.conversation_context import (
    ContextBudget,
    PromptContextWindow,
)


MAX_CANDIDATE_INPUT_DELTA_BYTES = 4096
MAX_CANDIDATE_SCHEMA_BYTES = 16_384
MAX_SAFE_JSON_INTEGER = 9_007_199_254_740_991
MAX_JSON_DEPTH = 16
MAX_JSON_CONTAINER_ITEMS = 128
MAX_JSON_TOTAL_NODES = 1024
_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}\Z")
_PROVIDER_REQUEST_ID_PATTERN = re.compile(
    r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}\Z"
)


def _require_non_blank(value: object, field_name: str) -> None:
    if type(value) is not str or not value.strip():
        raise ValueError(f"{field_name} must be non-blank text.")


def _plain_json(
    value: object,
    field_name: str,
    *,
    _depth: int = 0,
    _active: set[int] | None = None,
    _nodes: list[int] | None = None,
) -> object:
    if _depth > MAX_JSON_DEPTH:
        raise ValueError(f"{field_name} exceeds the safe depth limit.")
    active = set() if _active is None else _active
    nodes = [0] if _nodes is None else _nodes
    nodes[0] += 1
    if nodes[0] > MAX_JSON_TOTAL_NODES:
        raise ValueError(f"{field_name} exceeds the safe aggregate item limit.")
    if isinstance(value, Mapping):
        if len(value) > MAX_JSON_CONTAINER_ITEMS:
            raise ValueError(f"{field_name} exceeds the safe item limit.")
        identity = id(value)
        if identity in active:
            raise ValueError(f"{field_name} must not contain a cycle.")
        active.add(identity)
        result: dict[str, object] = {}
        try:
            for key, item in value.items():
                if type(key) is not str:
                    raise ValueError(f"{field_name} must use text keys.")
                result[key] = _plain_json(
                    item,
                    field_name,
                    _depth=_depth + 1,
                    _active=active,
                    _nodes=nodes,
                )
            return result
        finally:
            active.remove(identity)
    if isinstance(value, (list, tuple)):
        if len(value) > MAX_JSON_CONTAINER_ITEMS:
            raise ValueError(f"{field_name} exceeds the safe item limit.")
        identity = id(value)
        if identity in active:
            raise ValueError(f"{field_name} must not contain a cycle.")
        active.add(identity)
        try:
            return [
                _plain_json(
                    item,
                    field_name,
                    _depth=_depth + 1,
                    _active=active,
                    _nodes=nodes,
                )
                for item in value
            ]
        finally:
            active.remove(identity)
    if value is None or type(value) in (str, bool):
        return value
    if type(value) is int and abs(value) <= MAX_SAFE_JSON_INTEGER:
        return value
    if (
        type(value) is float
        and math.isfinite(value)
        and abs(value) <= MAX_SAFE_JSON_INTEGER
    ):
        return value
    raise ValueError(f"{field_name} must contain standard JSON values.")


def _freeze_json(value: object) -> object:
    if isinstance(value, Mapping):
        return MappingProxyType(
            {key: _freeze_json(item) for key, item in value.items()}
        )
    if isinstance(value, list):
        return tuple(_freeze_json(item) for item in value)
    return value


def _controlled_object(
    value: object,
    field_name: str,
    *,
    max_bytes: int,
) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{field_name} must be a JSON object.")
    plain = _plain_json(value, field_name)
    encoded = json.dumps(
        plain,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    if len(encoded) > max_bytes:
        raise ValueError(f"{field_name} exceeds the safe size limit.")
    frozen = _freeze_json(plain)
    assert isinstance(frozen, Mapping)
    return frozen


def _controlled_fields(value: object, field_name: str) -> tuple[str, ...]:
    if not isinstance(value, tuple) or len(value) > 32:
        raise ValueError(f"{field_name} must be a bounded tuple.")
    result: list[str] = []
    for item in value:
        _require_non_blank(item, f"{field_name} item")
        normalized = item.strip()
        if len(normalized.encode("utf-8")) > 128 or normalized in result:
            raise ValueError(f"{field_name} must contain controlled field names.")
        result.append(normalized)
    return tuple(result)


def _controlled_usage(
    value: Mapping[str, int] | None,
) -> Mapping[str, int] | None:
    if value is None:
        return None
    if set(value) != {"input_tokens", "output_tokens"} or any(
        type(item) is not int or item < 0 for item in value.values()
    ):
        raise ValueError("usage must use controlled token counters.")
    return MappingProxyType(dict(value))


@dataclass(frozen=True, slots=True)
class ToolInputExtractionInput:
    content_text: str
    tool_context_ref: ToolRef
    candidate_input_schema: Mapping[str, object]
    missing_fields: tuple[str, ...]
    ambiguous_fields: tuple[str, ...]
    context_window: PromptContextWindow = field(
        default_factory=PromptContextWindow
    )

    def __post_init__(self) -> None:
        _require_non_blank(self.content_text, "content_text")
        if not isinstance(self.tool_context_ref, ToolRef):
            raise ValueError("tool_context_ref must be a ToolRef.")
        schema = _controlled_object(
            self.candidate_input_schema,
            "candidate_input_schema",
            max_bytes=MAX_CANDIDATE_SCHEMA_BYTES,
        )
        missing = _controlled_fields(self.missing_fields, "missing_fields")
        ambiguous = _controlled_fields(
            self.ambiguous_fields,
            "ambiguous_fields",
        )
        if set(missing) & set(ambiguous):
            raise ValueError("A field cannot be both missing and ambiguous.")
        if not missing and not ambiguous:
            raise ValueError("A supplement requires an unresolved field.")
        if not isinstance(self.context_window, PromptContextWindow):
            raise ValueError("context_window must be a PromptContextWindow.")
        object.__setattr__(self, "candidate_input_schema", schema)
        object.__setattr__(self, "missing_fields", missing)
        object.__setattr__(self, "ambiguous_fields", ambiguous)


@dataclass(frozen=True, slots=True)
class ToolInputExtractionRequestMetadata:
    provider: str
    model_name: str
    prompt_template_id: str
    prompt_template_version: str
    prompt_digest: str
    generation_parameters: Mapping[str, object]

    def __post_init__(self) -> None:
        for field_name in (
            "provider",
            "model_name",
            "prompt_template_id",
            "prompt_template_version",
        ):
            _require_non_blank(getattr(self, field_name), field_name)
        if _SHA256_PATTERN.fullmatch(self.prompt_digest) is None:
            raise ValueError("prompt_digest must be lowercase SHA-256 hex.")
        object.__setattr__(
            self,
            "generation_parameters",
            _controlled_object(
                self.generation_parameters,
                "generation_parameters",
                max_bytes=MAX_CANDIDATE_INPUT_DELTA_BYTES,
            ),
        )


@dataclass(frozen=True, slots=True)
class ToolInputExtractionOutcome:
    candidate_input_delta: Mapping[str, object]
    request_metadata: ToolInputExtractionRequestMetadata
    usage: Mapping[str, int] | None = None
    provider_request_id: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(
            self.request_metadata,
            ToolInputExtractionRequestMetadata,
        ):
            raise ValueError(
                "request_metadata must be ToolInputExtractionRequestMetadata."
            )
        if (
            self.provider_request_id is not None
            and _PROVIDER_REQUEST_ID_PATTERN.fullmatch(
                self.provider_request_id
            )
            is None
        ):
            raise ValueError("provider_request_id is not controlled.")
        object.__setattr__(
            self,
            "candidate_input_delta",
            _controlled_object(
                self.candidate_input_delta,
                "candidate_input_delta",
                max_bytes=MAX_CANDIDATE_INPUT_DELTA_BYTES,
            ),
        )
        object.__setattr__(self, "usage", _controlled_usage(self.usage))


class ToolInputExtractionError(RuntimeError):
    default_error_code = "LLM_PROVIDER_UNAVAILABLE"
    default_safe_error_message = "LLM provider is unavailable."

    def __init__(
        self,
        message: str | None = None,
        *,
        error_code: str | None = None,
        safe_error_message: str | None = None,
        provider_request_id: str | None = None,
    ) -> None:
        controlled_message = (
            safe_error_message or self.default_safe_error_message
        )
        super().__init__(message or controlled_message)
        self.error_code = error_code or self.default_error_code
        self.safe_error_message = controlled_message
        self.provider_request_id = (
            provider_request_id
            if isinstance(provider_request_id, str)
            and _PROVIDER_REQUEST_ID_PATTERN.fullmatch(provider_request_id)
            else None
        )


class ToolInputExtractionTimeoutError(ToolInputExtractionError):
    default_error_code = "LLM_TIMEOUT"
    default_safe_error_message = "LLM provider request timed out."


class ToolInputExtractionProviderError(ToolInputExtractionError):
    pass


class ToolInputExtractionProtocolError(ToolInputExtractionError):
    default_error_code = "LLM_SCHEMA_MISMATCH"
    default_safe_error_message = (
        "LLM provider response did not match the required schema."
    )


class ToolInputExtractionPort(Protocol):
    provider: str
    model_name: str
    context_budget: ContextBudget

    def count_prompt_tokens(
        self,
        command: ToolInputExtractionInput,
    ) -> int: ...

    def request_metadata(
        self,
        command: ToolInputExtractionInput,
    ) -> ToolInputExtractionRequestMetadata: ...

    def extract(
        self,
        command: ToolInputExtractionInput,
    ) -> ToolInputExtractionOutcome: ...
