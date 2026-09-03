from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Literal, Protocol

from materialsagent.domain.ports.tool_registry import ToolRef


ContextRole = Literal["user", "assistant"]


def _plain_json(value: object) -> object:
    if isinstance(value, Mapping):
        result: dict[str, object] = {}
        for key, item in value.items():
            if type(key) is not str:
                raise ValueError("Context JSON objects must use text keys.")
            result[key] = _plain_json(item)
        return result
    if isinstance(value, (list, tuple)):
        return [_plain_json(item) for item in value]
    if value is None or type(value) in (str, bool, int, float):
        return value
    raise ValueError("Context values must be standard JSON values.")


def _freeze_json(value: object) -> object:
    if isinstance(value, Mapping):
        return MappingProxyType(
            {key: _freeze_json(item) for key, item in value.items()}
        )
    if isinstance(value, list):
        return tuple(_freeze_json(item) for item in value)
    return value


@dataclass(frozen=True, slots=True)
class ContextTurn:
    user_content: str
    assistant_content: str

    def __post_init__(self) -> None:
        if not self.user_content.strip() or not self.assistant_content.strip():
            raise ValueError("A context turn requires non-blank user and assistant text.")


@dataclass(frozen=True, slots=True)
class PromptContextWindow:
    summary_segments: tuple[str, ...] = ()
    recent_turns: tuple[ContextTurn, ...] = ()
    agent_state: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not all(
            isinstance(item, str) and item.strip()
            for item in self.summary_segments
        ):
            raise ValueError("summary_segments must contain non-blank text.")
        if not all(isinstance(item, ContextTurn) for item in self.recent_turns):
            raise ValueError("recent_turns must contain ContextTurn values.")
        if not isinstance(self.agent_state, Mapping):
            raise ValueError("agent_state must be a JSON object.")
        plain = _plain_json(self.agent_state)
        if not isinstance(plain, dict):
            raise ValueError("agent_state must be a JSON object.")
        object.__setattr__(self, "agent_state", _freeze_json(plain))


@dataclass(frozen=True, slots=True)
class ContextBudget:
    prompt_limit_tokens: int
    history_token_budget: int
    safety_margin_tokens: int
    context_window_tokens: int
    max_output_tokens: int

    def __post_init__(self) -> None:
        for field_name in (
            "prompt_limit_tokens",
            "context_window_tokens",
            "max_output_tokens",
        ):
            value = getattr(self, field_name)
            if type(value) is not int or value <= 0:
                raise ValueError(f"{field_name} must be a positive integer.")
        for field_name in ("history_token_budget", "safety_margin_tokens"):
            value = getattr(self, field_name)
            if type(value) is not int or value < 0:
                raise ValueError(f"{field_name} must be a nonnegative integer.")

    @property
    def effective_prompt_budget(self) -> int:
        return min(
            self.prompt_limit_tokens,
            self.context_window_tokens
            - self.max_output_tokens
            - self.safety_margin_tokens,
        )


@dataclass(frozen=True, slots=True)
class ContextReferenceResolution:
    context_ref: str
    conversation_id: str
    task_id: str
    task_input_revision_id: str
    tool_ref: ToolRef
    normalized_input: Mapping[str, object]

    def __post_init__(self) -> None:
        if not self.context_ref.startswith("ctx_ref_"):
            raise ValueError("context_ref must be a local Context Window reference.")
        for field_name in (
            "conversation_id",
            "task_id",
            "task_input_revision_id",
        ):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{field_name} must be non-blank text.")
        if not isinstance(self.tool_ref, ToolRef):
            raise ValueError("tool_ref must be a ToolRef.")
        plain = _plain_json(self.normalized_input)
        if not isinstance(plain, dict) or not plain:
            raise ValueError("normalized_input must be a non-empty JSON object.")
        object.__setattr__(self, "normalized_input", _freeze_json(plain))


@dataclass(frozen=True, slots=True)
class ContextSource:
    source_type: Literal[
        "MESSAGE",
        "TASK_INPUT_REVISION",
        "TOOL_RUN",
        "TOOL_RESULT",
        "EXPLANATION",
    ]
    task_id: str
    message_ids: tuple[str, ...]
    context_ref: str | None = None
    task_input_revision_id: str | None = None
    tool_run_id: str | None = None
    tool_result_id: str | None = None
    explanation_id: str | None = None
    projection_type: str | None = None
    projection_version: str | None = None


@dataclass(frozen=True, slots=True)
class ContextBuildResult:
    window: PromptContextWindow
    reference_resolutions: Mapping[str, ContextReferenceResolution]
    selected_sources: tuple[ContextSource, ...]
    candidate_turn_count: int
    selected_turn_count: int
    omitted_turn_count: int
    base_prompt_tokens: int
    history_tokens: int
    final_prompt_tokens: int
    budget: ContextBudget
    context_digest: str
    budget_exceeded: bool

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "reference_resolutions",
            MappingProxyType(dict(self.reference_resolutions)),
        )


class TokenCounter(Protocol):
    def count_text(self, value: str) -> int: ...

    def count_messages(self, messages: list[dict[str, str]]) -> int: ...
