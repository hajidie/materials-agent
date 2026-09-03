from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
import json
import math
import re
from types import MappingProxyType
from typing import Final, Literal, Protocol

from materialsagent.domain.ports.conversation_context import (
    ContextBudget,
    PromptContextWindow,
)
from materialsagent.domain.ports.tool_registry import RoutingCatalogSnapshot


KNOWLEDGE_ANSWER: Final = "KNOWLEDGE_ANSWER"
TOOL_EXECUTION: Final = "TOOL_EXECUTION"
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


@dataclass(frozen=True, slots=True)
class ChatOrchestrationInput:
    task_id: str
    conversation_id: str
    request_id: str
    content_text: str
    routing_catalog: RoutingCatalogSnapshot
    context_window: PromptContextWindow = field(
        default_factory=PromptContextWindow
    )

    def __post_init__(self) -> None:
        for field_name in (
            "task_id",
            "conversation_id",
            "request_id",
            "content_text",
        ):
            _require_non_blank(getattr(self, field_name), field_name)
        if not isinstance(
            self.routing_catalog,
            RoutingCatalogSnapshot,
        ):
            raise ValueError("routing_catalog must be a RoutingCatalogSnapshot.")
        if not isinstance(self.context_window, PromptContextWindow):
            raise ValueError("context_window must be a PromptContextWindow.")


@dataclass(frozen=True, slots=True)
class KnowledgeAnswer:
    answer_text: str
    route: Literal["KNOWLEDGE_ANSWER"] = field(
        init=False,
        default=KNOWLEDGE_ANSWER,
    )

    def __post_init__(self) -> None:
        _require_non_blank(self.answer_text, "answer_text")


def _plain_candidate_json(value: object) -> object:
    if isinstance(value, Mapping):
        result: dict[str, object] = {}
        for key, item in value.items():
            if type(key) is not str:
                raise ValueError("candidate_input must use text keys.")
            result[key] = _plain_candidate_json(item)
        return result
    if isinstance(value, (list, tuple)):
        return [_plain_candidate_json(item) for item in value]
    if _is_controlled_candidate_scalar(value):
        return value
    raise ValueError("candidate_input must contain safe JSON values.")


def _freeze_candidate_json(value: object) -> object:
    if isinstance(value, Mapping):
        return MappingProxyType(
            {key: _freeze_candidate_json(item) for key, item in value.items()}
        )
    if isinstance(value, list):
        return tuple(_freeze_candidate_json(item) for item in value)
    return value


@dataclass(frozen=True, slots=True)
class HistoryReference:
    context_ref: str
    reference_text: str

    def __post_init__(self) -> None:
        if re.fullmatch(r"ctx_ref_[0-9]{4,}", self.context_ref) is None:
            raise ValueError("context_ref must be a local Context Window reference.")
        _require_non_blank(self.reference_text, "reference_text")
        if len(self.reference_text.encode("utf-8")) > 1024:
            raise ValueError("reference_text exceeds the safe size limit.")


@dataclass(frozen=True, slots=True)
class ToolCandidateProposal:
    tool_id: str
    candidate_input_delta: Mapping[str, object]
    history_reference: HistoryReference | None = None

    def __post_init__(self) -> None:
        _require_non_blank(self.tool_id, "tool_id")
        if not isinstance(self.candidate_input_delta, Mapping):
            raise ValueError("candidate_input_delta must be a JSON object.")
        plain = _plain_candidate_json(self.candidate_input_delta)
        encoded = json.dumps(
            plain,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        if len(encoded) > 4096:
            raise ValueError("candidate_input_delta exceeds the safe size limit.")
        if self.history_reference is not None and not isinstance(
            self.history_reference,
            HistoryReference,
        ):
            raise ValueError("history_reference must be a HistoryReference.")
        object.__setattr__(
            self,
            "candidate_input_delta",
            _freeze_candidate_json(plain),
        )

    @property
    def candidate_input(self) -> Mapping[str, object]:
        """Read-only compatibility alias for pre-v6 internal callers."""
        return self.candidate_input_delta


@dataclass(frozen=True, slots=True)
class ToolCandidateSet:
    candidates: tuple[ToolCandidateProposal, ...]
    route: Literal["TOOL_CANDIDATES"] = field(
        init=False,
        default="TOOL_CANDIDATES",
    )

    def __post_init__(self) -> None:
        if not isinstance(self.candidates, tuple) or not 1 <= len(self.candidates) <= 5:
            raise ValueError("candidates must contain one to five proposals.")
        if not all(isinstance(item, ToolCandidateProposal) for item in self.candidates):
            raise ValueError("candidates must contain ToolCandidateProposal values.")
        if len({item.tool_id for item in self.candidates}) != len(self.candidates):
            raise ValueError("candidates must not repeat a Tool ID.")
        decoded = {
            "route": "TOOL_CANDIDATES",
            "candidates": [
                {
                    "tool_id": item.tool_id,
                    "candidate_input_delta": _plain_candidate_json(
                        item.candidate_input_delta
                    ),
                    **(
                        {}
                        if item.history_reference is None
                        else {
                            "history_reference": {
                                "context_ref": item.history_reference.context_ref,
                                "reference_text": item.history_reference.reference_text,
                            }
                        }
                    ),
                }
                for item in self.candidates
            ],
        }
        encoded = json.dumps(
            decoded,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        if len(encoded) > 4096:
            raise ValueError("Tool candidate set exceeds the safe size limit.")


ChatOrchestrationResult = (
    KnowledgeAnswer | ToolCandidateSet
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
            (KnowledgeAnswer, ToolCandidateSet),
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
    context_budget: ContextBudget

    def count_prompt_tokens(
        self,
        orchestration_input: ChatOrchestrationInput,
    ) -> int: ...

    def request_metadata(
        self,
        orchestration_input: ChatOrchestrationInput,
    ) -> ChatOrchestrationRequestMetadata: ...

    def orchestrate(
        self,
        orchestration_input: ChatOrchestrationInput,
    ) -> ChatOrchestrationOutcome: ...
