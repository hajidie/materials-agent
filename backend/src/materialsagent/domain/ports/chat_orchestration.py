from __future__ import annotations

from dataclasses import dataclass, field
import math
from typing import Final, Literal, Protocol


KNOWLEDGE_ANSWER: Final = "KNOWLEDGE_ANSWER"
TOOL_EXECUTION: Final = "TOOL_EXECUTION"
NEEDS_INPUT: Final = "NEEDS_INPUT"
ZTA35G_TOOL_ID: Final = "zta35g_sem_virtual_lab"
MAX_SAFE_JSON_INTEGER: Final = 9_007_199_254_740_991


class ChatOrchestrationTimeoutError(RuntimeError):
    """Safe timeout raised by a chat orchestration adapter."""


class ChatOrchestrationProviderError(RuntimeError):
    """Safe provider failure raised by a chat orchestration adapter."""


class ChatOrchestrationProtocolError(RuntimeError):
    """Safe structured-output protocol failure."""


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


class ChatOrchestrationPort(Protocol):
    provider: str
    model_name: str

    def orchestrate(
        self,
        orchestration_input: ChatOrchestrationInput,
    ) -> ChatOrchestrationResult: ...
