from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from enum import StrEnum
import json
import math
import re
from types import MappingProxyType

from materialsagent.domain.ports.tool_registry import ToolExecutionProfile, ToolRef


class ProposalOrigin(StrEnum):
    STRUCTURED = "STRUCTURED"
    NATIVE = "NATIVE"


class InvocationTrigger(StrEnum):
    MESSAGE_PROPOSAL = "MESSAGE_PROPOSAL"
    EXPLICIT_RETRY = "EXPLICIT_RETRY"


class InvocationStatus(StrEnum):
    PENDING = "PENDING"
    PENDING_CONFIRMATION = "PENDING_CONFIRMATION"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    DENIED = "DENIED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"
    OUTCOME_UNKNOWN = "OUTCOME_UNKNOWN"


TERMINAL_INVOCATION_STATUSES = frozenset(
    {
        InvocationStatus.SUCCEEDED,
        InvocationStatus.FAILED,
        InvocationStatus.DENIED,
        InvocationStatus.REJECTED,
        InvocationStatus.EXPIRED,
        InvocationStatus.OUTCOME_UNKNOWN,
    }
)
ALLOWED_INVOCATION_TRANSITIONS = {
    InvocationStatus.PENDING: frozenset(
        {
            InvocationStatus.PENDING_CONFIRMATION,
            InvocationStatus.RUNNING,
            InvocationStatus.DENIED,
            InvocationStatus.FAILED,
        }
    ),
    InvocationStatus.PENDING_CONFIRMATION: frozenset(
        {
            InvocationStatus.PENDING,
            InvocationStatus.REJECTED,
            InvocationStatus.EXPIRED,
        }
    ),
    InvocationStatus.RUNNING: frozenset(
        {
            InvocationStatus.SUCCEEDED,
            InvocationStatus.FAILED,
            InvocationStatus.OUTCOME_UNKNOWN,
        }
    ),
}
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")


def _require_text(value: object, field_name: str) -> str:
    if type(value) is not str or not value.strip():
        raise ValueError(f"{field_name} must be non-blank text.")
    return value.strip()


def _require_utc(value: datetime | None, field_name: str) -> None:
    if value is None:
        return
    if value.tzinfo is None or value.utcoffset() != timezone.utc.utcoffset(value):
        raise ValueError(f"{field_name} must be timezone-aware UTC.")


def _plain_json(value: object) -> object:
    if isinstance(value, Mapping):
        result: dict[str, object] = {}
        for key, item in value.items():
            if type(key) is not str:
                raise ValueError("JSON objects must use built-in text keys.")
            result[key] = _plain_json(item)
        return result
    if isinstance(value, (list, tuple)):
        return [_plain_json(item) for item in value]
    if value is None or type(value) in (str, bool, int):
        return value
    if type(value) is float and math.isfinite(value):
        return value
    raise ValueError("Value must contain controlled JSON.")


def _freeze_json(value: object) -> object:
    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze_json(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze_json(item) for item in value)
    return value


def _controlled_object(value: Mapping[str, object], field_name: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{field_name} must be a JSON object.")
    plain = _plain_json(value)
    encoded = json.dumps(
        plain,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    if len(encoded) > 16_384:
        raise ValueError(f"{field_name} exceeds the safe size limit.")
    frozen = _freeze_json(plain)
    assert isinstance(frozen, Mapping)
    return frozen


@dataclass(frozen=True, slots=True)
class ToolInvocationProposal:
    conversation_id: str
    source_message_id: str
    llm_call_id: str
    model_tool_name: str
    proposed_arguments: Mapping[str, object]
    origin: ProposalOrigin
    provider_tool_call_id: str | None = None

    def __post_init__(self) -> None:
        for field_name in (
            "conversation_id",
            "source_message_id",
            "llm_call_id",
            "model_tool_name",
        ):
            _require_text(getattr(self, field_name), field_name)
        if not isinstance(self.origin, ProposalOrigin):
            raise ValueError("origin is invalid.")
        if self.provider_tool_call_id is not None:
            _require_text(self.provider_tool_call_id, "provider_tool_call_id")
        object.__setattr__(
            self,
            "proposed_arguments",
            _controlled_object(self.proposed_arguments, "proposed_arguments"),
        )


@dataclass(frozen=True, slots=True)
class InvocationResult:
    invocation_result_id: str
    invocation_run_id: str
    data: Mapping[str, object]
    presentation: Mapping[str, object]
    created_at: datetime

    def __post_init__(self) -> None:
        _require_text(self.invocation_result_id, "invocation_result_id")
        _require_text(self.invocation_run_id, "invocation_run_id")
        _require_utc(self.created_at, "created_at")
        object.__setattr__(self, "data", _controlled_object(self.data, "data"))
        object.__setattr__(
            self,
            "presentation",
            _controlled_object(self.presentation, "presentation"),
        )


@dataclass(frozen=True, slots=True)
class InvocationRun:
    invocation_run_id: str
    actor_id: str
    conversation_id: str
    source_message_id: str
    task_id: str | None
    retry_of_invocation_run_id: str | None
    request_id: str
    idempotency_key: str
    trigger: InvocationTrigger
    tool_id: str
    tool_version: str
    schema_hash: str
    execution_profile: ToolExecutionProfile
    executor_id: str
    proposed_arguments: Mapping[str, object]
    policy_snapshot: Mapping[str, object]
    tool_projection: Mapping[str, object]
    status: InvocationStatus
    confirmation_required: bool
    confirmation_expires_at: datetime | None
    confirmed_at: datetime | None
    confirmed_by: str | None
    rejected_at: datetime | None
    rejected_by: str | None
    expired_at: datetime | None
    authorization_checked_at: datetime | None
    denied_at: datetime | None
    execution_claim_token: str | None
    execution_lease_expires_at: datetime | None
    dispatch_started_at: datetime | None
    execution_attempt_count: int
    invocation_result_id: str | None
    managed_tool_run_id: str | None
    error_code: str | None
    safe_error_message: str | None
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None

    def __post_init__(self) -> None:
        for field_name in (
            "invocation_run_id",
            "actor_id",
            "conversation_id",
            "source_message_id",
            "request_id",
            "idempotency_key",
            "tool_id",
            "tool_version",
            "executor_id",
        ):
            _require_text(getattr(self, field_name), field_name)
        for field_name in (
            "task_id",
            "retry_of_invocation_run_id",
            "confirmed_by",
            "rejected_by",
            "execution_claim_token",
            "invocation_result_id",
            "managed_tool_run_id",
            "error_code",
            "safe_error_message",
        ):
            value = getattr(self, field_name)
            if value is not None:
                _require_text(value, field_name)
        if _SHA256.fullmatch(self.schema_hash) is None:
            raise ValueError("schema_hash must be lowercase SHA-256 hex.")
        if not isinstance(self.trigger, InvocationTrigger):
            raise ValueError("trigger is invalid.")
        if not isinstance(self.execution_profile, ToolExecutionProfile):
            raise ValueError("execution_profile is invalid.")
        if not isinstance(self.status, InvocationStatus):
            raise ValueError("status is invalid.")
        if self.execution_profile is ToolExecutionProfile.MANAGED:
            if self.task_id is None or self.invocation_result_id is not None:
                raise ValueError("Managed Invocation relationship is invalid.")
        elif self.task_id is not None or self.managed_tool_run_id is not None:
            raise ValueError("Standard Invocation relationship is invalid.")
        if self.invocation_result_id is not None and self.managed_tool_run_id is not None:
            raise ValueError("Invocation result relationships are mutually exclusive.")
        if type(self.confirmation_required) is not bool:
            raise ValueError("confirmation_required must be boolean.")
        if (
            self.execution_profile is ToolExecutionProfile.SIDE_EFFECT
        ) is not self.confirmation_required:
            raise ValueError("Only Side-effect Invocations require confirmation.")
        if self.confirmation_required and self.confirmation_expires_at is None:
            raise ValueError("Confirmation expiry is required.")
        if not self.confirmation_required and any(
            value is not None
            for value in (
                self.confirmation_expires_at,
                self.confirmed_at,
                self.confirmed_by,
                self.rejected_at,
                self.rejected_by,
                self.expired_at,
            )
        ):
            raise ValueError("Non-confirmed Invocation cannot contain confirmation facts.")
        if (self.confirmed_at is None) != (self.confirmed_by is None):
            raise ValueError("Confirmation actor and time must be set together.")
        if (self.rejected_at is None) != (self.rejected_by is None):
            raise ValueError("Rejection actor and time must be set together.")
        if self.confirmed_at is not None and self.rejected_at is not None:
            raise ValueError("Confirmation and rejection facts are mutually exclusive.")
        if self.status is InvocationStatus.PENDING_CONFIRMATION and (
            not self.confirmation_required
            or self.confirmed_at is not None
            or self.rejected_at is not None
            or self.expired_at is not None
        ):
            raise ValueError("PENDING_CONFIRMATION facts are invalid.")
        if self.status is InvocationStatus.REJECTED and self.rejected_at is None:
            raise ValueError("REJECTED requires rejection facts.")
        if self.status is InvocationStatus.EXPIRED and self.expired_at is None:
            raise ValueError("EXPIRED requires expired_at.")
        if self.status is InvocationStatus.DENIED and self.denied_at is None:
            raise ValueError("DENIED requires denied_at.")
        if self.status is InvocationStatus.RUNNING and (
            self.execution_claim_token is None
            or self.execution_lease_expires_at is None
        ):
            raise ValueError("RUNNING requires an execution claim and lease.")
        if self.status is InvocationStatus.OUTCOME_UNKNOWN and (
            self.execution_profile is not ToolExecutionProfile.SIDE_EFFECT
        ):
            raise ValueError("OUTCOME_UNKNOWN is Side-effect-only.")
        if self.execution_attempt_count < 0:
            raise ValueError("execution_attempt_count must be nonnegative.")
        if self.dispatch_started_at is not None and (
            self.execution_attempt_count == 0 or self.execution_claim_token is None
        ):
            raise ValueError("Dispatch facts require an execution attempt and claim.")
        if (
            self.invocation_result_id is not None
            and self.status is not InvocationStatus.SUCCEEDED
        ):
            raise ValueError("InvocationResult is only valid for SUCCEEDED.")
        if self.status is InvocationStatus.SUCCEEDED:
            if self.execution_profile is ToolExecutionProfile.MANAGED:
                if self.managed_tool_run_id is None:
                    raise ValueError("Managed success requires ToolRun.")
            elif self.invocation_result_id is None:
                raise ValueError("Standard success requires InvocationResult.")
        if self.status in TERMINAL_INVOCATION_STATUSES and self.completed_at is None:
            raise ValueError("Terminal Invocation requires completed_at.")
        if self.status not in TERMINAL_INVOCATION_STATUSES and self.completed_at is not None:
            raise ValueError("Non-terminal Invocation cannot be completed.")
        for field_name in (
            "confirmation_expires_at",
            "confirmed_at",
            "rejected_at",
            "expired_at",
            "authorization_checked_at",
            "denied_at",
            "execution_lease_expires_at",
            "dispatch_started_at",
            "created_at",
            "updated_at",
            "completed_at",
        ):
            _require_utc(getattr(self, field_name), field_name)
        if self.updated_at < self.created_at:
            raise ValueError("updated_at must not be earlier than created_at.")
        object.__setattr__(
            self,
            "proposed_arguments",
            _controlled_object(self.proposed_arguments, "proposed_arguments"),
        )
        object.__setattr__(
            self,
            "policy_snapshot",
            _controlled_object(self.policy_snapshot, "policy_snapshot"),
        )
        object.__setattr__(
            self,
            "tool_projection",
            _controlled_object(self.tool_projection, "tool_projection"),
        )

    @property
    def tool_ref(self) -> ToolRef:
        return ToolRef(self.tool_id, self.tool_version, self.schema_hash)

    def transition(
        self,
        status: InvocationStatus,
        *,
        now: datetime,
        **changes: object,
    ) -> InvocationRun:
        if status not in ALLOWED_INVOCATION_TRANSITIONS.get(self.status, frozenset()):
            raise ValueError("Invocation status transition is not allowed.")
        return replace(self, status=status, updated_at=now, **changes)
