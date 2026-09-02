from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
import json
import math
from typing import TypeAlias
from types import MappingProxyType

from materialsagent.domain.ports.tool_execution import MaterialTool, ToolMetadata


class ToolStatus(StrEnum):
    ACTIVE = "ACTIVE"
    DEPRECATED = "DEPRECATED"
    DISABLED = "DISABLED"


class ExecutionPolicy(StrEnum):
    ANY_TASK = "ANY_TASK"
    EXISTING_TASK_ONLY = "EXISTING_TASK_ONLY"
    NONE = "NONE"


class ToolAction(StrEnum):
    NEW_BINDING = "NEW_BINDING"
    SUPPLEMENT = "SUPPLEMENT"
    EXECUTE = "EXECUTE"
    RETRY = "RETRY"


JsonObject: TypeAlias = Mapping[str, object]
ToolNormalizer: TypeAlias = Callable[[JsonObject, JsonObject | None], "ToolNormalization"]
MAX_SAFE_JSON_INTEGER = 9_007_199_254_740_991
MAX_NORMALIZED_INPUT_BYTES = 4096
MAX_SCHEMA_BYTES = 16_384
MAX_FOLLOW_UP_BYTES = 1024
MAX_JSON_DEPTH = 16
MAX_JSON_CONTAINER_ITEMS = 128
MAX_JSON_TOTAL_NODES = 1024
MAX_REQUESTED_OUTPUTS = 16
MAX_REQUESTED_OUTPUT_BYTES = 1024


@dataclass(frozen=True, slots=True)
class ToolRef:
    tool_id: str
    version: str
    schema_hash: str


@dataclass(frozen=True, slots=True)
class AuthorizationDecision:
    authorized_ref: ToolRef
    execution_policy_snapshot: ExecutionPolicy


@dataclass(frozen=True, slots=True)
class RoutingCatalogEntry:
    tool_id: str
    version: str
    schema_hash: str
    display_name: str
    description: str
    input_schema: JsonObject
    supported_outputs: tuple[str, ...]
    candidate_input_schema: JsonObject | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "input_schema",
            _controlled_json_object(
                self.input_schema,
                "input_schema",
                max_bytes=MAX_SCHEMA_BYTES,
            ),
        )
        candidate_schema = (
            self.input_schema
            if self.candidate_input_schema is None
            else self.candidate_input_schema
        )
        object.__setattr__(
            self,
            "candidate_input_schema",
            _controlled_json_object(
                candidate_schema,
                "candidate_input_schema",
                max_bytes=MAX_SCHEMA_BYTES,
            ),
        )

    @property
    def ref(self) -> ToolRef:
        return ToolRef(self.tool_id, self.version, self.schema_hash)


@dataclass(frozen=True, slots=True)
class RoutingCatalogSnapshot:
    entries: tuple[RoutingCatalogEntry, ...]


@dataclass(frozen=True, slots=True)
class ReadyNormalization:
    normalized_input: JsonObject
    requested_outputs: tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "normalized_input",
            _controlled_json_object(
                self.normalized_input,
                "normalized_input",
                max_bytes=MAX_NORMALIZED_INPUT_BYTES,
                require_non_empty=True,
            ),
        )
        object.__setattr__(
            self,
            "requested_outputs",
            _controlled_text_tuple(
                self.requested_outputs,
                "requested_outputs",
                require_non_empty=True,
                max_items=MAX_REQUESTED_OUTPUTS,
                max_bytes=MAX_REQUESTED_OUTPUT_BYTES,
            ),
        )


@dataclass(frozen=True, slots=True)
class NeedsInputNormalization:
    normalized_input: JsonObject
    missing_fields: tuple[str, ...]
    ambiguous_fields: tuple[str, ...]
    follow_up_suggestion: str

    def __post_init__(self) -> None:
        normalized = _controlled_json_object(
            self.normalized_input,
            "normalized_input",
            max_bytes=MAX_NORMALIZED_INPUT_BYTES,
            require_non_empty=True,
        )
        missing = _controlled_text_tuple(self.missing_fields, "missing_fields")
        ambiguous = _controlled_text_tuple(
            self.ambiguous_fields,
            "ambiguous_fields",
        )
        if not missing and not ambiguous:
            raise ValueError("NeedsInput normalization requires an unresolved field.")
        if set(missing) & set(ambiguous):
            raise ValueError("A field cannot be both missing and ambiguous.")
        if any(
            field_name not in normalized or normalized[field_name] is not None
            for field_name in (*missing, *ambiguous)
        ):
            raise ValueError("Unresolved fields must be present as null normalized values.")
        if type(self.follow_up_suggestion) is not str or not self.follow_up_suggestion.strip():
            raise ValueError("follow_up_suggestion must be non-blank text.")
        follow_up = self.follow_up_suggestion.strip()
        if len(follow_up.encode("utf-8")) > MAX_FOLLOW_UP_BYTES:
            raise ValueError("follow_up_suggestion exceeds the safe size limit.")
        object.__setattr__(self, "normalized_input", normalized)
        object.__setattr__(self, "missing_fields", missing)
        object.__setattr__(self, "ambiguous_fields", ambiguous)
        object.__setattr__(self, "follow_up_suggestion", follow_up)


ToolNormalization: TypeAlias = ReadyNormalization | NeedsInputNormalization


def _plain_standard_json(
    value: object,
    *,
    _depth: int = 0,
    _active: set[int] | None = None,
    _nodes: list[int] | None = None,
) -> object:
    if _depth > MAX_JSON_DEPTH:
        raise ValueError("JSON value exceeds the safe depth limit.")
    active = set() if _active is None else _active
    nodes = [0] if _nodes is None else _nodes
    nodes[0] += 1
    if nodes[0] > MAX_JSON_TOTAL_NODES:
        raise ValueError("JSON value exceeds the safe aggregate item limit.")
    if isinstance(value, Mapping):
        if len(value) > MAX_JSON_CONTAINER_ITEMS:
            raise ValueError("JSON object exceeds the safe item limit.")
        identity = id(value)
        if identity in active:
            raise ValueError("JSON value must not contain a cycle.")
        active.add(identity)
        try:
            result: dict[str, object] = {}
            for key, item in value.items():
                if type(key) is not str:
                    raise ValueError("JSON objects must use text keys.")
                result[key] = _plain_standard_json(
                    item,
                    _depth=_depth + 1,
                    _active=active,
                    _nodes=nodes,
                )
            return result
        finally:
            active.remove(identity)
    if isinstance(value, (list, tuple)):
        if len(value) > MAX_JSON_CONTAINER_ITEMS:
            raise ValueError("JSON array exceeds the safe item limit.")
        identity = id(value)
        if identity in active:
            raise ValueError("JSON value must not contain a cycle.")
        active.add(identity)
        try:
            return [
                _plain_standard_json(
                    item,
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
    if type(value) is float and math.isfinite(value) and abs(value) <= MAX_SAFE_JSON_INTEGER:
        return value
    raise ValueError("Value is not controlled standard JSON.")


def _freeze_json(
    value: object,
    *,
    _depth: int = 0,
    _active: set[int] | None = None,
    _nodes: list[int] | None = None,
) -> object:
    if _depth > MAX_JSON_DEPTH:
        raise ValueError("JSON value exceeds the safe depth limit.")
    active = set() if _active is None else _active
    nodes = [0] if _nodes is None else _nodes
    nodes[0] += 1
    if nodes[0] > MAX_JSON_TOTAL_NODES:
        raise ValueError("JSON value exceeds the safe aggregate item limit.")
    if isinstance(value, Mapping):
        if len(value) > MAX_JSON_CONTAINER_ITEMS:
            raise ValueError("JSON object exceeds the safe item limit.")
        identity = id(value)
        if identity in active:
            raise ValueError("JSON value must not contain a cycle.")
        active.add(identity)
        try:
            return MappingProxyType(
                {
                    key: _freeze_json(
                        item,
                        _depth=_depth + 1,
                        _active=active,
                        _nodes=nodes,
                    )
                    for key, item in value.items()
                }
            )
        finally:
            active.remove(identity)
    if isinstance(value, (list, tuple)):
        if len(value) > MAX_JSON_CONTAINER_ITEMS:
            raise ValueError("JSON array exceeds the safe item limit.")
        identity = id(value)
        if identity in active:
            raise ValueError("JSON value must not contain a cycle.")
        active.add(identity)
        try:
            return tuple(
                _freeze_json(
                    item,
                    _depth=_depth + 1,
                    _active=active,
                    _nodes=nodes,
                )
                for item in value
            )
        finally:
            active.remove(identity)
    return value


def _controlled_json_object(
    value: object,
    field_name: str,
    *,
    max_bytes: int,
    require_non_empty: bool = False,
) -> JsonObject:
    if not isinstance(value, Mapping):
        raise ValueError(f"{field_name} must be a JSON object.")
    plain = _plain_standard_json(value)
    if not isinstance(plain, dict) or (require_non_empty and not plain):
        raise ValueError(f"{field_name} must be a non-empty JSON object.")
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
    if not isinstance(frozen, Mapping):
        raise ValueError(f"{field_name} must be a JSON object.")
    return frozen


def _controlled_text_tuple(
    value: object,
    field_name: str,
    *,
    require_non_empty: bool = False,
    max_items: int = 32,
    max_bytes: int = 4096,
) -> tuple[str, ...]:
    if not isinstance(value, tuple):
        raise ValueError(f"{field_name} must be a tuple.")
    if len(value) > max_items:
        raise ValueError(f"{field_name} exceeds the safe item limit.")
    controlled: list[str] = []
    for item in value:
        if type(item) is not str or not item.strip() or len(item.encode("utf-8")) > 128:
            raise ValueError(f"{field_name} must contain controlled non-blank text.")
        normalized = item.strip()
        if normalized in controlled:
            raise ValueError(f"{field_name} must not contain duplicates.")
        controlled.append(normalized)
    if require_non_empty and not controlled:
        raise ValueError(f"{field_name} must not be empty.")
    aggregate_size = len(
        json.dumps(
            controlled,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
    )
    if aggregate_size > max_bytes:
        raise ValueError(f"{field_name} exceeds the safe size limit.")
    return tuple(controlled)


@dataclass(frozen=True, slots=True)
class ToolDefinition:
    tool_id: str
    version: str
    status: ToolStatus
    execution_policy: ExecutionPolicy
    display_name: str
    description: str
    input_schema: JsonObject
    runtime_metadata: ToolMetadata
    tool: MaterialTool
    supported_outputs: tuple[str, ...]
    supported_asset_types: tuple[str, ...]
    limitations: tuple[str, ...]
    normalizer: ToolNormalizer | None = None
    schema_hash: str = field(default="", compare=True)
    candidate_input_schema: JsonObject | None = None

    def __post_init__(self) -> None:
        input_schema = _freeze_json(self.input_schema)
        candidate_schema = _freeze_json(
            input_schema
            if self.candidate_input_schema is None
            else self.candidate_input_schema
        )
        object.__setattr__(self, "input_schema", input_schema)
        object.__setattr__(self, "candidate_input_schema", candidate_schema)

    @property
    def metadata(self) -> ToolMetadata:
        return self.runtime_metadata

    @property
    def ref(self) -> ToolRef:
        return ToolRef(self.tool_id, self.version, self.schema_hash)

    def normalize(
        self,
        candidate_input: JsonObject,
        prior_normalized_input: JsonObject | None = None,
    ) -> ToolNormalization:
        if self.normalizer is None:
            raise ValueError("Tool normalizer is unavailable.")
        return self.normalizer(candidate_input, prior_normalized_input)
