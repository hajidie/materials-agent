from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import Final

from materialsagent.domain.ports.chat_orchestration import (
    AmbiguousValue,
    ChatOrchestrationInput,
    ChatOrchestrationResult,
    KnowledgeAnswer,
    NeedsInputCandidate,
    ParameterCandidate,
    ToolCandidate,
    ZTA35GParameterCandidates,
)


MOCK_PROVIDER: Final = "mock"
MOCK_MODEL_NAME: Final = "mock-chat-orchestration-v1"
PARAMETER_FIELDS: Final = (
    "solution_temperature",
    "solution_time",
    "aging_temperature",
    "aging_time",
)


class ChatOrchestrationTimeoutError(RuntimeError):
    """Safe timeout raised by a chat orchestration adapter."""


class ChatOrchestrationProviderError(RuntimeError):
    """Safe provider failure raised by a chat orchestration adapter."""


class ChatOrchestrationProtocolError(RuntimeError):
    """Safe structured-output protocol failure."""


Responder = Callable[[ChatOrchestrationInput], Mapping[str, object]]


def _exact_keys(
    payload: Mapping[str, object],
    expected: set[str],
    field_name: str,
) -> None:
    if set(payload) != expected:
        raise ValueError(f"{field_name} has missing or unknown fields.")


def _sequence(value: object, field_name: str) -> tuple[object, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise ValueError(f"{field_name} must be an array.")
    return tuple(value)


def _string_sequence(value: object, field_name: str) -> tuple[str, ...]:
    items = _sequence(value, field_name)
    if not all(isinstance(item, str) for item in items):
        raise ValueError(f"{field_name} must be a string array.")
    return items  # type: ignore[return-value]


def _candidate_value(value: object) -> object:
    if not isinstance(value, Mapping):
        return value
    _exact_keys(value, {"candidates"}, "ambiguous candidate")
    return AmbiguousValue(_sequence(value["candidates"], "candidates"))


def _parameters(value: object) -> ZTA35GParameterCandidates:
    if not isinstance(value, Mapping):
        raise ValueError("candidate_parameters must be an object.")
    unknown = set(value) - set(PARAMETER_FIELDS)
    if unknown:
        raise ValueError("candidate_parameters has unknown fields.")

    decoded: dict[str, ParameterCandidate | None] = {}
    for field_name in PARAMETER_FIELDS:
        raw = value.get(field_name)
        if raw is None:
            decoded[field_name] = None
            continue
        if not isinstance(raw, Mapping):
            raise ValueError(f"{field_name} must be an object or null.")
        _exact_keys(raw, {"value", "unit"}, field_name)
        decoded[field_name] = ParameterCandidate(
            value=_candidate_value(raw["value"]),
            unit=_candidate_value(raw["unit"]),
        )
    return ZTA35GParameterCandidates(**decoded)


def _decode(payload: Mapping[str, object]) -> ChatOrchestrationResult:
    route = payload.get("route")
    if route == "KNOWLEDGE_ANSWER":
        _exact_keys(payload, {"route", "answer_text"}, "result")
        answer_text = payload["answer_text"]
        if not isinstance(answer_text, str):
            raise ValueError("answer_text must be text.")
        return KnowledgeAnswer(answer_text=answer_text)

    common = {
        "route",
        "tool_id",
        "material",
        "candidate_parameters",
        "requested_outputs",
    }
    if route == "TOOL_EXECUTION":
        _exact_keys(payload, common, "result")
        return ToolCandidate(
            tool_id=payload["tool_id"],  # type: ignore[arg-type]
            material=_candidate_value(payload["material"]),
            candidate_parameters=_parameters(payload["candidate_parameters"]),
            requested_outputs=_sequence(
                payload["requested_outputs"],
                "requested_outputs",
            ),
        )

    if route == "NEEDS_INPUT":
        _exact_keys(
            payload,
            common
            | {
                "missing_fields",
                "ambiguous_fields",
                "follow_up_suggestion",
            },
            "result",
        )
        follow_up = payload["follow_up_suggestion"]
        if not isinstance(follow_up, str):
            raise ValueError("follow_up_suggestion must be text.")
        return NeedsInputCandidate(
            tool_id=payload["tool_id"],  # type: ignore[arg-type]
            material=_candidate_value(payload["material"]),
            candidate_parameters=_parameters(payload["candidate_parameters"]),
            missing_fields=_string_sequence(
                payload["missing_fields"],
                "missing_fields",
            ),
            ambiguous_fields=_string_sequence(
                payload["ambiguous_fields"],
                "ambiguous_fields",
            ),
            follow_up_suggestion=follow_up,
            requested_outputs=_sequence(
                payload["requested_outputs"],
                "requested_outputs",
            ),
        )
    raise ValueError("route is unknown.")


class MockChatOrchestrationAdapter:
    provider = MOCK_PROVIDER
    model_name = MOCK_MODEL_NAME
    provider_request_id = None

    def __init__(self, responder: Responder) -> None:
        if not callable(responder):
            raise TypeError("responder must be callable.")
        self._responder = responder

    def orchestrate(
        self,
        orchestration_input: ChatOrchestrationInput,
    ) -> ChatOrchestrationResult:
        try:
            payload = self._responder(orchestration_input)
        except ChatOrchestrationTimeoutError:
            raise
        except ChatOrchestrationProviderError:
            raise
        except Exception:
            raise ChatOrchestrationProviderError(
                "Chat orchestration provider failed."
            ) from None
        if not isinstance(payload, Mapping):
            raise ChatOrchestrationProtocolError(
                "Chat orchestration result is not an object."
            )
        try:
            return _decode(payload)
        except Exception:
            raise ChatOrchestrationProtocolError(
                "Chat orchestration result violated the protocol."
            ) from None
