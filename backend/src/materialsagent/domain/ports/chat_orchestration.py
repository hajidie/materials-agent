from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
import math
import re
from types import MappingProxyType
from typing import Final, Literal, Protocol


KNOWLEDGE_ANSWER: Final = "KNOWLEDGE_ANSWER"
TOOL_EXECUTION: Final = "TOOL_EXECUTION"
NEEDS_INPUT: Final = "NEEDS_INPUT"
ZTA35G_TOOL_ID: Final = "zta35g_sem_virtual_lab"
MAX_SAFE_JSON_INTEGER: Final = 9_007_199_254_740_991
SHA256_PATTERN: Final = re.compile(r"[0-9a-f]{64}\Z")
PROVIDER_REQUEST_ID_PATTERN: Final = re.compile(
    r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}\Z"
)


class ChatOrchestrationError(RuntimeError):
    """Controlled chat orchestration failure safe for persistence."""

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
            if (
                provider_request_id is not None
                and PROVIDER_REQUEST_ID_PATTERN.fullmatch(provider_request_id)
                is not None
            )
            else None
        )


class ChatOrchestrationTimeoutError(ChatOrchestrationError):
    """Safe timeout raised by a chat orchestration adapter."""

    default_error_code = "LLM_TIMEOUT"
    default_safe_error_message = "LLM provider request timed out."


class ChatOrchestrationProviderError(ChatOrchestrationError):
    """Safe provider failure raised by a chat orchestration adapter."""


class ChatOrchestrationProtocolError(ChatOrchestrationError):
    """Safe structured-output protocol failure."""

    default_error_code = "LLM_SCHEMA_MISMATCH"
    default_safe_error_message = (
        "LLM provider response did not match the required schema."
    )


def _require_non_blank(value: str, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be non-blank text.")


def _is_controlled_candidate_scalar(value: object) -> bool:
    if value is None or type(value) in (str, bool):
        return True
    if type(value) is int:
        return abs(value) <= MAX_SAFE_JSON_INTEGER
    if type(value) is float:
        return math.isfinite(value) and abs(value) <= MAX_SAFE_JSON_INTEGER
    return False


def _require_candidate_value(value: object, field_name: str) -> None:
    if isinstance(value, AmbiguousValue):
        return
    if not _is_controlled_candidate_scalar(value):
        raise ValueError(
            f"{field_name} candidate must be a controlled JSON scalar."
        )


@dataclass(frozen=True, slots=True)
class ChatOrchestrationInput:
    task_id: str
    conversation_id: str
    request_id: str
    content_text: str

    def __post_init__(self) -> None:
        for field_name in (
            "task_id",
            "conversation_id",
            "request_id",
            "content_text",
        ):
            _require_non_blank(getattr(self, field_name), field_name)


@dataclass(frozen=True, slots=True)
class AmbiguousValue:
    candidates: tuple[object, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.candidates, tuple):
            raise ValueError(
                "AmbiguousValue candidates must be a tuple."
            )
        deduplicated: list[object] = []
        for candidate in self.candidates:
            if not _is_controlled_candidate_scalar(candidate):
                raise ValueError(
                    "AmbiguousValue candidate must be a controlled JSON scalar."
                )
            if not any(
                type(candidate) is type(existing) and candidate == existing
                for existing in deduplicated
            ):
                deduplicated.append(candidate)
        if len(deduplicated) < 2:
            raise ValueError(
                "AmbiguousValue requires at least two distinct candidates."
            )
        object.__setattr__(self, "candidates", tuple(deduplicated))


@dataclass(frozen=True, slots=True)
class ParameterCandidate:
    value: object
    unit: object

    def __post_init__(self) -> None:
        _require_candidate_value(self.value, "value")
        _require_candidate_value(self.unit, "unit")


@dataclass(frozen=True, slots=True)
class ZTA35GParameterCandidates:
    solution_temperature: ParameterCandidate | None = None
    solution_time: ParameterCandidate | None = None
    aging_temperature: ParameterCandidate | None = None
    aging_time: ParameterCandidate | None = None

    def __post_init__(self) -> None:
        for field_name in (
            "solution_temperature",
            "solution_time",
            "aging_temperature",
            "aging_time",
        ):
            value = getattr(self, field_name)
            if value is not None and not isinstance(value, ParameterCandidate):
                raise ValueError(
                    "candidate_parameters must contain ParameterCandidate "
                    "values or null."
                )


@dataclass(frozen=True, slots=True)
class KnowledgeAnswer:
    answer_text: str
    route: Literal["KNOWLEDGE_ANSWER"] = field(
        init=False,
        default=KNOWLEDGE_ANSWER,
    )

    def __post_init__(self) -> None:
        _require_non_blank(self.answer_text, "answer_text")


@dataclass(frozen=True, slots=True)
class ToolCandidate:
    tool_id: str
    material: object
    candidate_parameters: ZTA35GParameterCandidates
    requested_outputs: tuple[object, ...]
    route: Literal["TOOL_EXECUTION"] = field(
        init=False,
        default=TOOL_EXECUTION,
    )

    def __post_init__(self) -> None:
        if self.tool_id != ZTA35G_TOOL_ID:
            raise ValueError(
                f"tool_id must be {ZTA35G_TOOL_ID}."
            )
        if not isinstance(
            self.candidate_parameters,
            ZTA35GParameterCandidates,
        ):
            raise ValueError(
                "candidate_parameters must be ZTA35GParameterCandidates."
            )
        _require_candidate_value(self.material, "material")
        if not isinstance(self.requested_outputs, tuple):
            raise ValueError("requested_outputs must be a tuple.")
        if not all(
            _is_controlled_candidate_scalar(item)
            for item in self.requested_outputs
        ):
            raise ValueError(
                "requested_outputs must contain controlled JSON scalars."
            )


@dataclass(frozen=True, slots=True)
class NeedsInputCandidate:
    tool_id: str
    material: object
    candidate_parameters: ZTA35GParameterCandidates
    missing_fields: tuple[str, ...]
    ambiguous_fields: tuple[str, ...]
    follow_up_suggestion: str
    requested_outputs: tuple[object, ...]
    route: Literal["NEEDS_INPUT"] = field(
        init=False,
        default=NEEDS_INPUT,
    )

    def __post_init__(self) -> None:
        if self.tool_id != ZTA35G_TOOL_ID:
            raise ValueError(
                f"tool_id must be {ZTA35G_TOOL_ID}."
            )
        if not isinstance(
            self.candidate_parameters,
            ZTA35GParameterCandidates,
        ):
            raise ValueError(
                "candidate_parameters must be ZTA35GParameterCandidates."
            )
        _require_candidate_value(self.material, "material")
        for field_name in ("missing_fields", "ambiguous_fields"):
            value = getattr(self, field_name)
            if not isinstance(value, tuple) or not all(
                isinstance(item, str) for item in value
            ):
                raise ValueError(f"{field_name} must be a string tuple.")
        _require_non_blank(
            self.follow_up_suggestion,
            "follow_up_suggestion",
        )
        if not isinstance(self.requested_outputs, tuple):
            raise ValueError("requested_outputs must be a tuple.")
        if not all(
            _is_controlled_candidate_scalar(item)
            for item in self.requested_outputs
        ):
            raise ValueError(
                "requested_outputs must contain controlled JSON scalars."
            )


ChatOrchestrationResult = (
    KnowledgeAnswer | ToolCandidate | NeedsInputCandidate
)


def _controlled_usage(
    value: Mapping[str, int] | None,
) -> Mapping[str, int] | None:
    if value is None:
        return None
    if set(value) != {"input_tokens", "output_tokens"} or any(
        type(item) is not int or item < 0
        for item in value.values()
    ):
        raise ValueError("usage must use controlled token counters.")
    return MappingProxyType(dict(value))


@dataclass(frozen=True, slots=True)
class ChatOrchestrationRequestMetadata:
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
        if SHA256_PATTERN.fullmatch(self.prompt_digest) is None:
            raise ValueError("prompt_digest must be lowercase SHA-256 hex.")
        if not isinstance(self.generation_parameters, Mapping):
            raise ValueError("generation_parameters must be an object.")
        object.__setattr__(
            self,
            "generation_parameters",
            MappingProxyType(dict(self.generation_parameters)),
        )


@dataclass(frozen=True, slots=True)
class ChatOrchestrationOutcome:
    result: ChatOrchestrationResult
    usage: Mapping[str, int] | None
    provider_request_id: str | None

    def __post_init__(self) -> None:
        if not isinstance(
            self.result,
            (KnowledgeAnswer, ToolCandidate, NeedsInputCandidate),
        ):
            raise ValueError("result must be a chat orchestration result.")
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
        object.__setattr__(self, "usage", _controlled_usage(self.usage))


class ChatOrchestrationPort(Protocol):
    provider: str
    model_name: str

    def request_metadata(
        self,
        orchestration_input: ChatOrchestrationInput,
    ) -> ChatOrchestrationRequestMetadata: ...

    def orchestrate(
        self,
        orchestration_input: ChatOrchestrationInput,
    ) -> ChatOrchestrationOutcome: ...
