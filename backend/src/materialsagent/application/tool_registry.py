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
    ToolDefinition,
    ToolRef,
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

    def __init__(self, definitions: tuple[ToolDefinition, ...]) -> None:
        if not definitions:
            raise InvalidToolRegistrationError("At least one Tool definition is required.")
        registrations: dict[str, ToolDefinition] = {}
        for definition in definitions:
            validated = self._validate(definition)
            if validated.tool_id in registrations:
                raise InvalidToolRegistrationError("Duplicate Tool registration.")
            registrations[validated.tool_id] = validated
        self._registrations = registrations

    @staticmethod
    def _validate(definition: ToolDefinition) -> ToolDefinition:
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
        if definition.tool.metadata != definition.runtime_metadata:
            raise InvalidToolRegistrationError("Tool runtime metadata must be Registry-owned.")
        if definition.execution_policy is not ExecutionPolicy.NONE and not callable(definition.normalizer):
            raise InvalidToolRegistrationError("Executable Tool registration is incomplete.")
        if definition.context_projector is not None and not callable(
            definition.context_projector
        ):
            raise InvalidToolRegistrationError("Tool context projector is invalid.")
        try:
            input_schema_bytes = _canonical_schema_bytes(definition.input_schema)
            candidate_schema = definition.candidate_input_schema or {}
            candidate_schema_bytes = _canonical_schema_bytes(candidate_schema)
            if len(input_schema_bytes) > MAX_SCHEMA_BYTES:
                raise ValueError("Tool input schema exceeds the safe size limit.")
            if len(candidate_schema_bytes) > MAX_SCHEMA_BYTES:
                raise ValueError("Tool candidate schema exceeds the safe size limit.")
            schema_hash = hashlib.sha256(input_schema_bytes).hexdigest()
        except (TypeError, ValueError):
            raise InvalidToolRegistrationError("Tool input schema is invalid.") from None
        return replace(definition, schema_hash=schema_hash)

    def resolve(self, tool_id: str, *, snapshot: RoutingCatalogSnapshot | None = None) -> ToolDefinition:
        definition = self._registrations.get(tool_id)
        if definition is None:
            raise UnknownToolError("Unknown Tool.")
        if snapshot is not None:
            matching = [entry for entry in snapshot.entries if entry.tool_id == tool_id]
            if len(matching) != 1 or matching[0].ref != definition.ref:
                raise UnknownToolError("Tool is not in the routing snapshot.")
        return definition

    def list_registered(self) -> tuple[ToolDefinition, ...]:
        return tuple(self._registrations[tool_id] for tool_id in sorted(self._registrations))

    def routing_snapshot(self) -> RoutingCatalogSnapshot:
        return RoutingCatalogSnapshot(
            entries=tuple(
                RoutingCatalogEntry(
                    tool_id=definition.tool_id,
                    version=definition.version,
                    schema_hash=definition.schema_hash,
                    display_name=definition.display_name,
                    description=definition.description,
                    input_schema=definition.input_schema,
                    candidate_input_schema=definition.candidate_input_schema,
                    supported_outputs=definition.supported_outputs,
                )
                for definition in self.list_registered()
                if definition.status is ToolStatus.ACTIVE
                and definition.execution_policy is ExecutionPolicy.ANY_TASK
            )
        )

    def authorize(
        self,
        *,
        registration: ToolDefinition,
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
