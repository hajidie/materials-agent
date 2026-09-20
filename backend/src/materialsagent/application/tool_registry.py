from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from enum import StrEnum
import hashlib
import json
import re

from materialsagent.domain.ports.tool_registry import (
    AuthorizationDecision,
    ExecutionPolicy,
    MAX_SCHEMA_BYTES,
    RoutingCatalogEntry,
    RoutingCatalogSnapshot,
    ToolAction,
    ExecutionMode,
    RegisteredTool,
    ToolRef,
    ToolExecutionProfile,
    PresentationMode,
    ResourceProvider,
    ResourceType,
    ToolStatus,
)


class InvalidToolRegistrationError(ValueError):
    """A ToolDefinition is unsafe or incomplete for startup registration."""


class UnknownToolError(LookupError):
    """Requested Tool is not in the process-local Registry."""


class ToolAuthorizationDenialReason(StrEnum):
    BOUND_TOOL_MISMATCH = "BOUND_TOOL_MISMATCH"
    CURRENT_REGISTRATION_MISMATCH = "CURRENT_REGISTRATION_MISMATCH"
    POLICY_ACTION_DENIED = "POLICY_ACTION_DENIED"
    SCHEMA_DRIFT = "SCHEMA_DRIFT"


class ToolAuthorizationError(PermissionError):
    """The current Registry denied a Tool action."""

    def __init__(self, reason: ToolAuthorizationDenialReason) -> None:
        super().__init__("Tool action is not authorized by the current Registry.")
        self.reason = reason


_TOOL_ID = re.compile(r"^[a-z][a-z0-9_]{0,127}$")
_APPROVED_LIFECYCLE_PAIRS = {
    (ToolStatus.ACTIVE, ExecutionPolicy.ANY_TASK),
    (ToolStatus.DEPRECATED, ExecutionPolicy.EXISTING_TASK_ONLY),
    (ToolStatus.DISABLED, ExecutionPolicy.NONE),
}


def _json_value(value: object) -> object:
    if isinstance(value, Mapping):
        result: dict[str, object] = {}
        for key, item in value.items():
            if type(key) is not str:
                raise ValueError("JSON objects must use text keys.")
            result[key] = _json_value(item)
        return result
    if isinstance(value, list | tuple):
        return [_json_value(item) for item in value]
    return value


def _canonical_schema_bytes(input_schema: Mapping[str, object]) -> bytes:
    if not isinstance(input_schema, Mapping):
        raise ValueError("Tool input schema must be a JSON object.")
    try:
        return json.dumps(
            _json_value(input_schema),
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (RecursionError, TypeError, ValueError):
        raise ValueError("Tool input schema is not canonical JSON.") from None


def canonical_schema_hash(input_schema: Mapping[str, object]) -> str:
    payload = _canonical_schema_bytes(input_schema)
    return hashlib.sha256(payload).hexdigest()


class ToolRegistry:
    """Immutable, explicit process-local Tool registration boundary."""

    def __init__(self, registrations: tuple[RegisteredTool, ...]) -> None:
        if not registrations:
            raise InvalidToolRegistrationError("At least one Tool definition is required.")
        validated_registrations: dict[str, RegisteredTool] = {}
        for registration in registrations:
            validated = self._validate(registration)
            if validated.tool_id in validated_registrations:
                raise InvalidToolRegistrationError("Duplicate Tool registration.")
            validated_registrations[validated.tool_id] = validated
        self._registrations = validated_registrations

    @staticmethod
    def _validate(registration: RegisteredTool) -> RegisteredTool:
        if not isinstance(registration, RegisteredTool):
            raise InvalidToolRegistrationError("RegisteredTool is required.")
        definition = registration.definition
        if not _TOOL_ID.fullmatch(definition.tool_id):
            raise InvalidToolRegistrationError("Tool ID is invalid.")
        if not isinstance(definition.version, str) or not definition.version.strip():
            raise InvalidToolRegistrationError("Tool version is invalid.")
        if not isinstance(definition.status, ToolStatus) or not isinstance(
            definition.execution_policy,
            ExecutionPolicy,
        ):
            raise InvalidToolRegistrationError("Tool lifecycle values are invalid.")
        if (definition.status, definition.execution_policy) not in _APPROVED_LIFECYCLE_PAIRS:
            raise InvalidToolRegistrationError("Tool lifecycle pair is invalid.")
        if definition.runtime_metadata.tool_id != definition.tool_id:
            raise InvalidToolRegistrationError("Runtime metadata Tool ID does not match.")
        target_metadata = getattr(registration.binding.execution_target, "metadata", None)
        if (
            definition.execution_profile is ToolExecutionProfile.MANAGED
            and target_metadata != definition.runtime_metadata
        ):
            raise InvalidToolRegistrationError("Tool runtime metadata must be Registry-owned.")
        if (
            definition.execution_profile is ToolExecutionProfile.MANAGED
            and definition.execution_policy is not ExecutionPolicy.NONE
            and not callable(registration.binding.normalizer)
        ):
            raise InvalidToolRegistrationError("Executable Tool registration is incomplete.")
        if definition.execution_mode is ExecutionMode.ASYNC:
            raise InvalidToolRegistrationError("ASYNC_TOOL_UNSUPPORTED")
        policy = definition.tool_execution_policy
        if (
            definition.execution_profile is ToolExecutionProfile.SIDE_EFFECT
            and (
                not policy.confirmation_required
                or not policy.idempotency_required
                or not policy.audit_required
                or not policy.required_permissions
                or definition.confirmation_prompt is None
            )
        ):
            raise InvalidToolRegistrationError(
                "Side-effect Tool controls are incomplete."
            )
        if (
            definition.execution_profile is not ToolExecutionProfile.MANAGED
            and (
                registration.binding.validator is None
                or registration.binding.codec is None
                or registration.binding.presenter is None
            )
        ):
            raise InvalidToolRegistrationError("Standard Tool binding is incomplete.")
        if (
            definition.execution_profile is not ToolExecutionProfile.MANAGED
            and definition.presentation_mode is PresentationMode.LLM_SUMMARY
        ):
            raise InvalidToolRegistrationError(
                "Standard Tool presentation mode is not implemented."
            )
        if registration.binding.context_projector is not None and not callable(
            registration.binding.context_projector
        ):
            raise InvalidToolRegistrationError("Tool context projector is invalid.")
        input_properties = definition.input_schema.get("properties", {})
        input_required = set(definition.input_schema.get("required", ()))
        if not isinstance(input_properties, Mapping) or not isinstance(
            definition.input_schema.get("required", ()), (list, tuple)
        ):
            raise InvalidToolRegistrationError("Tool input schema is invalid.")
        parameter_names = {item.model_argument for item in definition.resource_parameters}
        execution_names = {item.execution_argument for item in definition.resource_parameters}
        if (len(parameter_names) != len(definition.resource_parameters)
                or len(execution_names) != len(definition.resource_parameters)):
            raise InvalidToolRegistrationError("Duplicate resource parameter declaration.")
        if parameter_names & execution_names:
            raise InvalidToolRegistrationError("Resource model and execution arguments must be distinct.")
        if parameter_names & (set(input_properties) - execution_names):
            raise InvalidToolRegistrationError("Resource model argument collides with an execution field.")
        for item in definition.resource_parameters:
            if not _TOOL_ID.fullmatch(item.model_argument) or not _TOOL_ID.fullmatch(item.execution_argument):
                raise InvalidToolRegistrationError("Resource parameter name is invalid.")
            if item.execution_argument not in input_properties:
                raise InvalidToolRegistrationError("Resource execution argument is missing from the input schema.")
            if item.required != (item.execution_argument in input_required):
                raise InvalidToolRegistrationError("Resource parameter requiredness does not match the input schema.")
            if item.provider is ResourceProvider.ASSET and item.expected_resource_type is not ResourceType.EBSD_IMAGE:
                raise InvalidToolRegistrationError("Asset resource type is unsupported.")
            if item.provider is ResourceProvider.ML_RESOURCE and item.expected_resource_type is ResourceType.EBSD_IMAGE:
                raise InvalidToolRegistrationError("ML resource type is unsupported.")
        try:
            input_schema_bytes = _canonical_schema_bytes(definition.input_schema)
            proposal_schema_bytes = _canonical_schema_bytes(definition.proposal_schema or {})
            output_schema_bytes = _canonical_schema_bytes(definition.output_schema)
            if len(input_schema_bytes) > MAX_SCHEMA_BYTES:
                raise ValueError("Tool input schema exceeds the safe size limit.")
            if len(proposal_schema_bytes) > MAX_SCHEMA_BYTES:
                raise ValueError("Tool proposal schema exceeds the safe size limit.")
            if len(output_schema_bytes) > MAX_SCHEMA_BYTES:
                raise ValueError("Tool output schema exceeds the safe size limit.")
            hash_input = input_schema_bytes
            if definition.resource_parameters:
                resource_contract = [
                    {
                        "model_argument": item.model_argument,
                        "execution_argument": item.execution_argument,
                        "expected_resource_type": item.expected_resource_type.value,
                        "provider": item.provider.value,
                        "required": item.required,
                    }
                    for item in definition.resource_parameters
                ]
                hash_input += _canonical_schema_bytes({"resource_parameters": resource_contract})
            schema_hash = hashlib.sha256(hash_input).hexdigest()
        except (TypeError, ValueError):
            raise InvalidToolRegistrationError("Tool input schema is invalid.") from None
        return replace(
            registration,
            definition=replace(definition, schema_hash=schema_hash),
        )

    def resolve(self, tool_id: str, *, snapshot: RoutingCatalogSnapshot | None = None) -> RegisteredTool:
        registration = self._registrations.get(tool_id)
        if registration is None:
            raise UnknownToolError("Unknown Tool.")
        if snapshot is not None:
            matching = [entry for entry in snapshot.entries if entry.tool_id == tool_id]
            if len(matching) != 1 or matching[0].ref != registration.ref:
                raise UnknownToolError("Tool is not in the routing snapshot.")
        return registration

    def list_registered(self) -> tuple[RegisteredTool, ...]:
        return tuple(self._registrations[tool_id] for tool_id in sorted(self._registrations))

    def routing_snapshot(self) -> RoutingCatalogSnapshot:
        return RoutingCatalogSnapshot(
            entries=tuple(
                RoutingCatalogEntry(
                    tool_id=registration.tool_id,
                    version=registration.version,
                    schema_hash=registration.schema_hash,
                    display_name=registration.display_name,
                    description=registration.description,
                    input_schema=registration.input_schema,
                    candidate_input_schema=registration.proposal_schema,
                    supported_outputs=registration.supported_outputs,
                )
                for registration in self.list_registered()
                if registration.status is ToolStatus.ACTIVE
                and registration.execution_policy is ExecutionPolicy.ANY_TASK
            )
        )

    def authorize(
        self,
        *,
        registration: RegisteredTool,
        action: ToolAction,
        bound_ref: ToolRef | None,
    ) -> AuthorizationDecision:
        current = self._registrations.get(registration.tool_id)
        if current is not registration:
            raise ToolAuthorizationError(
                ToolAuthorizationDenialReason.CURRENT_REGISTRATION_MISMATCH
            )
        if action is ToolAction.NEW_BINDING:
            if bound_ref is not None:
                raise ToolAuthorizationError(
                    ToolAuthorizationDenialReason.BOUND_TOOL_MISMATCH
                )
            if current.execution_policy is not ExecutionPolicy.ANY_TASK:
                raise ToolAuthorizationError(
                    ToolAuthorizationDenialReason.POLICY_ACTION_DENIED
                )
            return AuthorizationDecision(
                authorized_ref=current.ref,
                execution_policy_snapshot=current.execution_policy,
            )
        if bound_ref is None or bound_ref.tool_id != current.tool_id:
            raise ToolAuthorizationError(
                ToolAuthorizationDenialReason.BOUND_TOOL_MISMATCH
            )
        if bound_ref.schema_hash != current.schema_hash:
            raise ToolAuthorizationError(
                ToolAuthorizationDenialReason.SCHEMA_DRIFT
            )
        if (
            action not in {
                ToolAction.SUPPLEMENT,
                ToolAction.EXECUTE,
                ToolAction.RETRY,
            }
            or current.execution_policy
            not in {
                ExecutionPolicy.ANY_TASK,
                ExecutionPolicy.EXISTING_TASK_ONLY,
            }
        ):
            raise ToolAuthorizationError(
                ToolAuthorizationDenialReason.POLICY_ACTION_DENIED
            )
        return AuthorizationDecision(
            authorized_ref=current.ref,
            execution_policy_snapshot=current.execution_policy,
        )
