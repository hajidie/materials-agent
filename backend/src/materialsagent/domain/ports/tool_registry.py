from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
import json
import math
from typing import TypeAlias
from types import MappingProxyType

from materialsagent.domain.ports.tool_execution import ToolMetadata


class ToolStatus(StrEnum):
    ACTIVE = "ACTIVE"
    DEPRECATED = "DEPRECATED"
    DISABLED = "DISABLED"


class ToolLifecyclePolicy(StrEnum):
    ANY_TASK = "ANY_TASK"
    EXISTING_TASK_ONLY = "EXISTING_TASK_ONLY"
    NONE = "NONE"


# Backward-compatible import name. New code should use ToolLifecyclePolicy.
ExecutionPolicy = ToolLifecyclePolicy


class ToolExecutionProfile(StrEnum):
    STANDARD = "STANDARD"
    SIDE_EFFECT = "SIDE_EFFECT"
    MANAGED = "MANAGED"


class ExecutionMode(StrEnum):
    SYNC = "SYNC"
    ASYNC = "ASYNC"


class PresentationMode(StrEnum):
    DETERMINISTIC = "DETERMINISTIC"
    LLM_SUMMARY = "LLM_SUMMARY"
    CUSTOM = "CUSTOM"


class ToolAction(StrEnum):
    NEW_BINDING = "NEW_BINDING"
    SUPPLEMENT = "SUPPLEMENT"
    EXECUTE = "EXECUTE"
    RETRY = "RETRY"


JsonObject: TypeAlias = Mapping[str, object]
ToolNormalizer: TypeAlias = Callable[[JsonObject, JsonObject | None], "ToolNormalization"]
ToolContextProjector: TypeAlias = Callable[[JsonObject], JsonObject]
ToolInputValidator: TypeAlias = Callable[[JsonObject], JsonObject]
ToolResultCodec: TypeAlias = Callable[[object], JsonObject]
ToolResultPresenter: TypeAlias = Callable[[JsonObject], JsonObject]
ToolConfirmationPreviewBuilder: TypeAlias = Callable[[JsonObject], JsonObject]
ToolHealthProbe: TypeAlias = Callable[[], str]
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
class ToolExecutionPolicy:
    lifecycle_policy: ToolLifecyclePolicy = ToolLifecyclePolicy.ANY_TASK
    required_permissions: tuple[str, ...] = ()
    confirmation_required: bool = False
    confirmation_ttl_seconds: int = 900
    idempotency_required: bool = True
    audit_required: bool = True

    def __post_init__(self) -> None:
        if not isinstance(self.lifecycle_policy, ToolLifecyclePolicy):
            raise ValueError("lifecycle_policy is invalid.")
        permissions = _controlled_text_tuple(
            self.required_permissions,
            "required_permissions",
        )
        if type(self.confirmation_required) is not bool:
            raise ValueError("confirmation_required must be boolean.")
        if (
            type(self.confirmation_ttl_seconds) is not int
            or self.confirmation_ttl_seconds <= 0
            or self.confirmation_ttl_seconds > 86_400
        ):
            raise ValueError("confirmation_ttl_seconds is invalid.")
        if type(self.idempotency_required) is not bool or type(self.audit_required) is not bool:
            raise ValueError("execution control flags must be boolean.")
        object.__setattr__(self, "required_permissions", permissions)


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


@dataclass(frozen=True, slots=True)
class InvalidNormalization:
    normalized_input: JsonObject
    validation_errors: tuple[JsonObject, ...]

    def __post_init__(self) -> None:
        normalized = _controlled_json_object(
            self.normalized_input,
            "normalized_input",
            max_bytes=MAX_NORMALIZED_INPUT_BYTES,
            require_non_empty=True,
        )
        if not isinstance(self.validation_errors, tuple) or not self.validation_errors:
            raise ValueError("validation_errors must be a non-empty tuple.")
        if len(self.validation_errors) > 32:
            raise ValueError("validation_errors exceeds the safe item limit.")
        controlled = tuple(
            _controlled_json_object(
                error,
                "validation_errors item",
                max_bytes=1024,
                require_non_empty=True,
            )
            for error in self.validation_errors
        )
        object.__setattr__(self, "normalized_input", normalized)
        object.__setattr__(self, "validation_errors", controlled)


ToolNormalization: TypeAlias = (
    ReadyNormalization | NeedsInputNormalization | InvalidNormalization
)


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
    display_name: str
    description: str
    input_schema: JsonObject
    runtime_metadata: ToolMetadata
    supported_outputs: tuple[str, ...]
    supported_asset_types: tuple[str, ...]
    limitations: tuple[str, ...]
    execution_profile: ToolExecutionProfile = ToolExecutionProfile.MANAGED
    execution_mode: ExecutionMode = ExecutionMode.SYNC
    executor_id: str = "managed_runtime"
    tool_execution_policy: ToolExecutionPolicy = field(
        default_factory=ToolExecutionPolicy
    )
    presentation_mode: PresentationMode = PresentationMode.DETERMINISTIC
    presenter_id: str = "deterministic"
    schema_hash: str = field(default="", compare=True)
    proposal_schema: JsonObject | None = None
    output_schema: JsonObject = field(default_factory=dict)
    context_projection_version: str = "metadata-only-v1"
    confirmation_prompt: str | None = None

    def __post_init__(self) -> None:
        input_schema = _controlled_json_object(
            self.input_schema,
            "input_schema",
            max_bytes=MAX_SCHEMA_BYTES,
        )
        proposal_schema = _controlled_json_object(
            input_schema if self.proposal_schema is None else self.proposal_schema,
            "proposal_schema",
            max_bytes=MAX_SCHEMA_BYTES,
        )
        output_schema = _controlled_json_object(
            self.output_schema,
            "output_schema",
            max_bytes=MAX_SCHEMA_BYTES,
        )
        object.__setattr__(self, "input_schema", input_schema)
        object.__setattr__(self, "proposal_schema", proposal_schema)
        object.__setattr__(self, "output_schema", output_schema)
        if not isinstance(self.execution_profile, ToolExecutionProfile):
            raise ValueError("execution_profile is invalid.")
        if not isinstance(self.execution_mode, ExecutionMode):
            raise ValueError("execution_mode is invalid.")
        if not isinstance(self.tool_execution_policy, ToolExecutionPolicy):
            raise ValueError("tool_execution_policy is invalid.")
        if not isinstance(self.presentation_mode, PresentationMode):
            raise ValueError("presentation_mode is invalid.")
        for field_name in ("executor_id", "presenter_id"):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{field_name} must be non-blank text.")
        if (
            not isinstance(self.context_projection_version, str)
            or not self.context_projection_version.strip()
        ):
            raise ValueError("context_projection_version must be non-blank text.")
        if self.confirmation_prompt is not None and (
            type(self.confirmation_prompt) is not str
            or not self.confirmation_prompt.strip()
            or len(self.confirmation_prompt.encode("utf-8")) > 512
        ):
            raise ValueError("confirmation_prompt must be controlled text.")

    @property
    def metadata(self) -> ToolMetadata:
        return self.runtime_metadata

    @property
    def execution_policy(self) -> ToolLifecyclePolicy:
        """Compatibility view of the former lifecycle-only field."""

        return self.tool_execution_policy.lifecycle_policy

    @property
    def candidate_input_schema(self) -> JsonObject:
        """Compatibility name for the model-facing proposal schema."""

        assert self.proposal_schema is not None
        return self.proposal_schema

    @property
    def ref(self) -> ToolRef:
        return ToolRef(self.tool_id, self.version, self.schema_hash)


@dataclass(frozen=True, slots=True)
class ToolExecutionBinding:
    execution_target: object
    normalizer: ToolNormalizer | None = None
    validator: ToolInputValidator | None = None
    context_projector: ToolContextProjector | None = None
    codec: ToolResultCodec | None = None
    presenter: ToolResultPresenter | None = None
    confirmation_preview_builder: ToolConfirmationPreviewBuilder | None = None
    health_probe: ToolHealthProbe | None = None

    def __post_init__(self) -> None:
        if self.execution_target is None:
            raise ValueError("execution_target is required.")
        for field_name in (
            "normalizer",
            "validator",
            "context_projector",
            "codec",
            "presenter",
            "confirmation_preview_builder",
            "health_probe",
        ):
            value = getattr(self, field_name)
            if value is not None and not callable(value):
                raise ValueError(f"{field_name} must be callable.")


@dataclass(frozen=True, slots=True)
class RegisteredTool:
    definition: ToolDefinition
    binding: ToolExecutionBinding

    @property
    def tool_id(self) -> str:
        return self.definition.tool_id

    @property
    def version(self) -> str:
        return self.definition.version

    @property
    def status(self) -> ToolStatus:
        return self.definition.status

    @property
    def execution_policy(self) -> ToolLifecyclePolicy:
        return self.definition.execution_policy

    @property
    def execution_profile(self) -> ToolExecutionProfile:
        return self.definition.execution_profile

    @property
    def execution_mode(self) -> ExecutionMode:
        return self.definition.execution_mode

    @property
    def executor_id(self) -> str:
        return self.definition.executor_id

    @property
    def display_name(self) -> str:
        return self.definition.display_name

    @property
    def description(self) -> str:
        return self.definition.description

    @property
    def input_schema(self) -> JsonObject:
        return self.definition.input_schema

    @property
    def candidate_input_schema(self) -> JsonObject:
        return self.definition.candidate_input_schema

    @property
    def proposal_schema(self) -> JsonObject:
        return self.definition.candidate_input_schema

    @property
    def output_schema(self) -> JsonObject:
        return self.definition.output_schema

    @property
    def runtime_metadata(self) -> ToolMetadata:
        return self.definition.runtime_metadata

    @property
    def metadata(self) -> ToolMetadata:
        return self.definition.runtime_metadata

    @property
    def supported_outputs(self) -> tuple[str, ...]:
        return self.definition.supported_outputs

    @property
    def supported_asset_types(self) -> tuple[str, ...]:
        return self.definition.supported_asset_types

    @property
    def limitations(self) -> tuple[str, ...]:
        return self.definition.limitations

    @property
    def schema_hash(self) -> str:
        return self.definition.schema_hash

    @property
    def context_projection_version(self) -> str:
        return self.definition.context_projection_version

    @property
    def ref(self) -> ToolRef:
        return self.definition.ref

    @property
    def tool(self) -> object:
        """Compatibility access to the execution target, never metadata."""

        return self.binding.execution_target

    @property
    def normalizer(self) -> ToolNormalizer | None:
        return self.binding.normalizer

    @property
    def context_projector(self) -> ToolContextProjector | None:
        return self.binding.context_projector

    def normalize(
        self,
        candidate_input: JsonObject,
        prior_normalized_input: JsonObject | None = None,
    ) -> ToolNormalization:
        if self.binding.normalizer is None:
            raise ValueError("Tool normalizer is unavailable.")
        return self.binding.normalizer(candidate_input, prior_normalized_input)
