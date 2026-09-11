from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
import re
from typing import Final

from materialsagent.domain.ports.chat_orchestration import (
    ChatOrchestrationProtocolError,
    ChatOrchestrationProviderError,
    ChatOrchestrationInput,
    ChatOrchestrationOutcome,
    ChatOrchestrationRequestMetadata,
    ChatOrchestrationResult,
    ChatOrchestrationTimeoutError,
    HistoryReference,
    KnowledgeAnswer,
    ToolCandidateProposal,
    ToolCandidateSet,
)
from materialsagent.domain.ports.conversation_context import ContextBudget
from materialsagent.infrastructure.llm.common import canonical_prompt_digest
from materialsagent.infrastructure.llm.prompts import render_chat_orchestration_prompt
from materialsagent.infrastructure.llm.token_counter import Cl100kTokenCounter


MOCK_PROVIDER: Final = "mock"
MOCK_MODEL_NAME: Final = "mock-chat-orchestration-v1"
PROMPT_TEMPLATE_ID: Final = "chat-orchestration"
PROMPT_TEMPLATE_VERSION: Final = "6"
GENERATION_PARAMETERS: Final = {"temperature": 0, "max_tokens": 256}
_AGING_TEMPERATURE_SUPPLEMENT_PATTERN: Final = re.compile(
    r"(?:aging_temperature\s*=\s*|时效温度\s+)?730\s*°\s*C",
    re.IGNORECASE,
)


Responder = Callable[[ChatOrchestrationInput], Mapping[str, object]]


def _default_parameters() -> dict[str, object]:
    return {
        "solution_temperature": {"value": 1000, "unit": "°C"},
        "solution_time": {"value": 3, "unit": "h"},
        "aging_temperature": {"value": 730, "unit": "°C"},
        "aging_time": {"value": 3, "unit": "h"},
    }


def _default_tool_payload() -> dict[str, object]:
    return {
        "route": "TOOL_CANDIDATES",
        "candidates": [
            {
                "tool_id": "zta35g_sem_virtual_lab",
                "proposed_arguments": {
                    "material": "ZTA35G",
                    **_default_parameters(),
                    "requested_outputs": ["sem_image", "mechanical_properties"],
                },
            }
        ],
    }


def _is_aging_temperature_supplement(content: str) -> bool:
    return (
        _AGING_TEMPERATURE_SUPPLEMENT_PATTERN.fullmatch(content.strip())
        is not None
    )


def default_mock_responder(
    orchestration_input: ChatOrchestrationInput,
) -> Mapping[str, object]:
    """Deterministic local responder for the approved M4 acceptance phrases."""
    content = orchestration_input.content_text.strip()
    lowered = content.casefold()
    if "mock timeout" in lowered:
        raise ChatOrchestrationTimeoutError(
            "Chat orchestration mock timed out."
        )
    if "mock provider failure" in lowered:
        raise ChatOrchestrationProviderError(
            "Chat orchestration mock provider failed."
        )
    if "mock protocol failure" in lowered:
        return {"route": "UNKNOWN"}
    if "缺 aging_temperature" in content:
        parameters = _default_parameters()
        parameters["aging_temperature"] = None
        return {
            "route": "TOOL_CANDIDATES",
            "candidates": [{
                "tool_id": "zta35g_sem_virtual_lab",
                "proposed_arguments": {
                    "material": "ZTA35G",
                    **parameters,
                    "requested_outputs": ["sem_image", "mechanical_properties"],
                },
            }],
        }
    if _is_aging_temperature_supplement(content):
        payload = _default_tool_payload()
        parameters = _default_parameters()
        parameters["aging_temperature"] = {"value": 730, "unit": "°C"}
        payload["candidates"][0]["proposed_arguments"].update(parameters)  # type: ignore[index,union-attr]
        return payload
    if "歧义" in content:
        parameters = _default_parameters()
        parameters["solution_time"] = {
            "value": {"candidates": [2, 3]},
            "unit": "h",
        }
        return {
            "route": "TOOL_CANDIDATES",
            "candidates": [{
                "tool_id": "zta35g_sem_virtual_lab",
                "proposed_arguments": {
                    "material": "ZTA35G",
                    **parameters,
                    "requested_outputs": ["sem_image"],
                },
            }],
        }
    payload = _default_tool_payload()
    if "solution_time = 180 min" in lowered:
        parameters = _default_parameters()
        parameters["solution_time"] = {"value": 180, "unit": "min"}
        payload["candidates"][0]["proposed_arguments"].update(parameters)  # type: ignore[index,union-attr]
        return payload
    if "越界温度" in content:
        parameters = _default_parameters()
        parameters["solution_temperature"] = {"value": 1200, "unit": "°C"}
        payload["candidates"][0]["proposed_arguments"].update(parameters)  # type: ignore[index,union-attr]
        return payload
    if "完整合法" in content:
        return payload
    return {
        "route": "KNOWLEDGE_ANSWER",
        "answer_text": "ZTA35G 是氧化锆增韧氧化铝复合材料。",
    }


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


def _decode(payload: Mapping[str, object]) -> ChatOrchestrationResult:
    route = payload.get("route")
    if route == "KNOWLEDGE_ANSWER":
        _exact_keys(payload, {"route", "answer_text"}, "result")
        answer_text = payload["answer_text"]
        if not isinstance(answer_text, str):
            raise ValueError("answer_text must be text.")
        return KnowledgeAnswer(answer_text=answer_text)

    if route == "TOOL_CANDIDATES":
        _exact_keys(payload, {"route", "candidates"}, "result")
        candidates = _sequence(payload["candidates"], "candidates")
        decoded: list[ToolCandidateProposal] = []
        for item in candidates:
            if not isinstance(item, Mapping):
                raise ValueError("candidate must be an object.")
            if set(item) not in (
                {"tool_id", "proposed_arguments"},
                {"tool_id", "proposed_arguments", "history_reference"},
            ):
                raise ValueError("candidate has missing or unknown fields.")
            proposed_arguments = item.get("proposed_arguments")
            if not isinstance(item["tool_id"], str) or not isinstance(
                proposed_arguments, Mapping
            ):
                raise ValueError("candidate has invalid fields.")
            history_reference = item.get("history_reference")
            if history_reference is not None:
                if (
                    not isinstance(history_reference, Mapping)
                    or set(history_reference) != {"context_ref", "reference_text"}
                    or not all(
                        isinstance(history_reference[key], str)
                        for key in ("context_ref", "reference_text")
                    )
                ):
                    raise ValueError("history_reference has invalid fields.")
            decoded.append(
                ToolCandidateProposal(
                    tool_id=item["tool_id"],
                    proposed_arguments=proposed_arguments,
                    history_reference=(
                        None
                        if history_reference is None
                        else HistoryReference(
                            context_ref=history_reference["context_ref"],
                            reference_text=history_reference["reference_text"],
                        )
                    ),
                )
            )
        return ToolCandidateSet(tuple(decoded))
    raise ValueError("route is unknown.")


class MockChatOrchestrationAdapter:
    provider = MOCK_PROVIDER
    model_name = MOCK_MODEL_NAME
    context_budget = ContextBudget(
        prompt_limit_tokens=16_384,
        history_token_budget=8_192,
        safety_margin_tokens=1_024,
        context_window_tokens=1_000_000,
        max_output_tokens=256,
    )

    def __init__(self, responder: Responder) -> None:
        if not callable(responder):
            raise TypeError("responder must be callable.")
        self._responder = responder
        self._token_counter = Cl100kTokenCounter()

    def count_prompt_tokens(
        self,
        orchestration_input: ChatOrchestrationInput,
    ) -> int:
        from materialsagent.infrastructure.llm.langchain_chat import (
            ProviderChatResponse,
        )

        return self._token_counter.count_messages(
            render_chat_orchestration_prompt(orchestration_input)
        ) + self._token_counter.count_schema(
            ProviderChatResponse.model_json_schema()
        )

    def request_metadata(
        self,
        orchestration_input: ChatOrchestrationInput,
    ) -> ChatOrchestrationRequestMetadata:
        messages = render_chat_orchestration_prompt(orchestration_input)
        return ChatOrchestrationRequestMetadata(
            provider=self.provider,
            model_name=self.model_name,
            prompt_template_id=PROMPT_TEMPLATE_ID,
            prompt_template_version=PROMPT_TEMPLATE_VERSION,
            prompt_digest=canonical_prompt_digest(
                template_id=PROMPT_TEMPLATE_ID,
                template_version=PROMPT_TEMPLATE_VERSION,
                messages=messages,
            ),
            generation_parameters=GENERATION_PARAMETERS,
        )

    def orchestrate(
        self,
        orchestration_input: ChatOrchestrationInput,
    ) -> ChatOrchestrationOutcome:
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
            result = _decode(payload)
        except Exception:
            raise ChatOrchestrationProtocolError(
                "Chat orchestration result violated the protocol."
            ) from None
        return ChatOrchestrationOutcome(
            result=result,
            usage=None,
            provider_request_id=None,
        )
