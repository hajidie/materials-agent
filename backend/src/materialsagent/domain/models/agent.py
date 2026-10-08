"""Provider-independent, durable contracts for the bounded Agent runtime."""
from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
import json
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator


def now() -> datetime:
    return datetime.now(timezone.utc)


def identifier() -> str:
    return str(uuid4())


def canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def fingerprint(value: Any) -> str:
    return sha256(canonical(value).encode("utf-8")).hexdigest()


def tool_call_key(agent_run_id: str, tool_call_id: str) -> str:
    return "agent-tool-call:" + fingerprint([agent_run_id, tool_call_id])


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)


class RunBudget(Contract):
    max_model_calls: int = Field(default=12, ge=1, le=100)
    max_tool_executions: int = Field(default=4, ge=1, le=32)
    max_active_seconds: float = Field(default=3600, gt=0, le=86400)
    max_llm_tokens: int = Field(default=64000, ge=1, le=2000000)
    standard_timeout_seconds: float = Field(default=10, gt=0, le=3600)
    managed_timeout_seconds: float = Field(default=1200, gt=0, le=86400)


class PendingQuestion(Contract):
    question: str = Field(min_length=1, max_length=2048)


class TokenUsage(Contract):
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    reasoning_tokens: int = Field(default=0, ge=0)
    total_tokens: int = Field(gt=0)
    source: Literal["actual", "estimated"]
    estimator_version: str | None = None

    @model_validator(mode="after")
    def valid_total(self):
        if self.total_tokens < self.input_tokens + self.output_tokens:
            raise ValueError("Usage total cannot omit observable input/output.")
        if self.source == "estimated" and not self.estimator_version:
            raise ValueError("Estimated usage requires an estimator version.")
        return self


class ModelCall(Contract):
    call_id: str = Field(default_factory=identifier)
    logical_call_id: str | None = None
    attempt_no: int = Field(default=1, ge=1)
    failure_category: str | None = None
    role: Literal["agent_decision"]
    status: Literal["RUNNING", "SUCCEEDED", "FAILED"] = "RUNNING"
    prompt_digest: str
    output_limit: int = Field(ge=1)
    input_reserved: int = Field(default=0, ge=0)
    usage: TokenUsage | None = None
    error_code: str | None = None
    created_at: datetime = Field(default_factory=now)


class Observation(Contract):
    unit_annotations: list[dict[str, Any]] = Field(default_factory=list, max_length=16)
    observation_id: str = Field(default_factory=identifier)
    tool_call_id: str
    kind: Literal["TOOL_RESULT", "ARGUMENT_RESOLUTION"]
    status: str
    tool_name: str
    data: dict[str, Any] = Field(default_factory=dict)
    invocation_run_id: str | None = None
    task_id: str | None = None
    tool_run_id: str | None = None
    result_id: str | None = None
    source_agent_run_id: str | None = None
    presentation: dict[str, Any] = Field(default_factory=dict)
    result_summary: dict[str, Any] | None = None
    artifacts: list[dict[str, Any]] = Field(default_factory=list)
    warnings: list[Any] = Field(default_factory=list)
    error: dict[str, Any] | None = None
    created_at: datetime = Field(default_factory=now)

    def agent_projection(self) -> dict[str, Any]:
        # Keep all result facts once; result_summary duplicates these for UI rendering.
        projection = self.model_dump(mode="json", exclude={"result_summary"})
        if self.result_summary:
            projection["result_metadata"] = {key: value for key, value in self.result_summary.items()
                if key not in {"data", "warnings", "error", "status", "result_id", "tool_run_id", "created_at"}}
        return projection


class ResourceBinding(Contract):
    version: Literal["resource-binding-v1"] = "resource-binding-v1"
    provider: Literal["ml_resource", "asset"]
    resource_type: Literal["dataset", "training_run", "model", "prediction", "ebsd_image"]
    model_argument: str = Field(min_length=1, max_length=128)
    execution_argument: str = Field(min_length=1, max_length=128)
    platform_resource_id: str = Field(min_length=1, max_length=128)
    execution_value: str = Field(min_length=1, max_length=512)
    identity_digest: str | None = Field(default=None, min_length=1, max_length=256)
    content_digest: str | None = Field(default=None, min_length=1, max_length=256)
    safe_description: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def provider_identity(self):
        if self.provider == "ml_resource" and (not self.identity_digest or self.content_digest is not None):
            raise ValueError("ML resource binding requires only an identity digest.")
        if self.provider == "asset" and (not self.content_digest or self.identity_digest is not None):
            raise ValueError("Asset binding requires only a content digest.")
        return self


class ArgumentDraft(Contract):
    unit_annotations: list[dict[str, Any]] = Field(default_factory=list, max_length=16)
    resource_bindings: dict[str, ResourceBinding] = Field(default_factory=dict)
    tool_name: str
    version: str
    schema_hash: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    normalized: dict[str, Any] = Field(default_factory=dict)
    issues: dict[str, Literal["Missing", "Invalid", "Conflict", "Ambiguous"]] = Field(default_factory=dict)
    task_id: str | None = None
    revision_id: str | None = None
    resolver_authoritative: bool = False


class ExecutionRecord(Contract):
    unit_annotations: list[dict[str, Any]] = Field(default_factory=list, max_length=16)
    resource_bindings: dict[str, ResourceBinding] = Field(default_factory=dict)
    tool_call_id: str
    tool_name: str
    version: str
    schema_hash: str
    arguments: dict[str, Any]
    execution_fingerprint: str
    invocation_run_id: str | None = None
    status: str = "PENDING"
    confirmation_version: str | None = None
    confirmation_expires_at: datetime | None = None
    confirmed: bool = False
    dispatched: bool = False
    retryable: bool = False
    retry_of_invocation_run_id: str | None = None
    observation_id: str | None = None
    task_id: str | None = None
    tool_run_id: str | None = None
    created_at: datetime = Field(default_factory=now)


class AgentRun(Contract):
    resource_protocol_version: Literal["resource-ref-v1"] = "resource-ref-v1"
    agent_run_id: str = Field(default_factory=identifier)
    conversation_id: str
    actor_id: str
    source_message_id: str
    status: Literal["PENDING", "RUNNING", "WAITING_FOR_USER", "WAITING_FOR_CONFIRMATION", "INTERRUPTED", "SUCCEEDED", "TERMINATED"] = "PENDING"
    version: int = 0
    claim: str | None = None
    process_id: str | None = None
    budget: RunBudget = Field(default_factory=RunBudget)
    active_seconds: float = 0
    llm_tokens: int = 0
    tool_executions: int = 0
    waiting_version: int = 0
    submission_id: str | None = None
    accepted_submission_id: str | None = Field(default=None, exclude=True)
    question_message_id: str | None = None
    final_message_id: str | None = None
    source_answer_message_id: str | None = None
    answer_root_message_id: str | None = None
    stop_requested_at: datetime | None = None
    draft: ArgumentDraft | None = None
    pending_execution: ExecutionRecord | None = None
    observations: list[Observation] = Field(default_factory=list)
    executions: list[ExecutionRecord] = Field(default_factory=list)
    calls: list[ModelCall] = Field(default_factory=list)
    user_message_ids: list[str] = Field(default_factory=list)
    context_message_ids: list[str] = Field(default_factory=list)
    messages: list[dict[str, Any]] = Field(default_factory=list, exclude=True)
    pending_message: dict[str, Any] | None = Field(default=None, exclude=True)
    current_unit_assertions: list[dict[str, str]] = Field(default_factory=list, exclude=True)
    attachments: list[dict[str, Any]] = Field(default_factory=list, max_length=1)
    result_attachments: list[dict[str, Any]] = Field(default_factory=list, exclude=True)
    error_code: str | None = None
    pending_tool_call_id: str | None = None
    pending_resource_map: dict[str, dict[str, Any]] = Field(default_factory=dict)
    answered_questions: dict[str, str] = Field(default_factory=dict)
    confirmation_response: dict[str, Any] | None = None
    recovery_replay: bool = False
    resumed_version: int | None = None
    retry_not_before: datetime | None = None
    pending_model_retry: dict[str, Any] | None = None
    persistence_operation_id: str | None = None
    duplicate_of_invocation_run_id: str | None = None
    source_agent_run_id: str | None = None
    retry_type: Literal["TOOL_RETRY", "ANSWER_REGENERATION"] | None = None
    retry_execution: ExecutionRecord | None = None
    tool_execution_disabled: bool = False
    created_at: datetime = Field(default_factory=now)
    updated_at: datetime = Field(default_factory=now)

    @property
    def terminal(self) -> bool:
        return self.status in {"SUCCEEDED", "TERMINATED"}

    @property
    def goal(self) -> str:
        return next((m["text"] for m in self.messages if m["message_id"] == self.source_message_id), "待处理请求")

    @property
    def user_messages(self) -> list[dict[str, Any]]:
        by_id = {m["message_id"]: m for m in self.messages}
        return [by_id[i] for i in self.user_message_ids if i in by_id]

    @property
    def user_inputs(self) -> list[str]:
        return [m["text"] for m in self.user_messages if m["message_id"] != self.source_message_id]

    @property
    def context(self) -> list[dict[str, Any]]:
        by_id = {m["message_id"]: m for m in self.messages}
        return [{"role": by_id[i]["role"].lower(), "content": by_id[i]["text"],
                 "additional_inputs": by_id[i].get("attachments", [])}
                for i in self.context_message_ids if i in by_id]

    @property
    def waiting(self) -> PendingQuestion | None:
        message = next((m for m in self.messages if m["message_id"] == self.question_message_id), None)
        return PendingQuestion(question=message["text"]) if message else None

    def validate_transition(self, previous: AgentRun) -> None:
        allowed = {
            "PENDING": {"RUNNING", "INTERRUPTED", "TERMINATED"},
            "RUNNING": {"RUNNING", "WAITING_FOR_USER", "WAITING_FOR_CONFIRMATION", "INTERRUPTED", "SUCCEEDED", "TERMINATED"},
            "INTERRUPTED": {"PENDING", "INTERRUPTED", "TERMINATED"},
            "WAITING_FOR_USER": {"PENDING", "RUNNING", "TERMINATED"},
            "WAITING_FOR_CONFIRMATION": {"PENDING", "RUNNING", "TERMINATED"},
        }
        if self.status not in allowed.get(previous.status, set()):
            raise ValueError("AgentRun transition is invalid.")
        if self.status == "SUCCEEDED" and self.final_message_id is None:
            raise ValueError("Successful AgentRun requires a published answer message.")
        if self.budget != previous.budget or self.llm_tokens < previous.llm_tokens or self.tool_executions < previous.tool_executions:
            raise ValueError("Run budgets cannot reset.")
