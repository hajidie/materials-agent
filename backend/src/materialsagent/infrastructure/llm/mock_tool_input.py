from __future__ import annotations

from collections.abc import Callable, Mapping
import re
from typing import Final

from materialsagent.domain.ports.tool_input_extraction import (
    ToolInputExtractionInput,
    ToolInputExtractionOutcome,
    ToolInputExtractionProtocolError,
    ToolInputExtractionProviderError,
    ToolInputExtractionRequestMetadata,
    ToolInputExtractionTimeoutError,
)
from materialsagent.domain.ports.conversation_context import ContextBudget
from materialsagent.infrastructure.llm.common import canonical_prompt_digest
from materialsagent.infrastructure.llm.prompts import render_tool_input_extraction_prompt
from materialsagent.infrastructure.llm.token_counter import Cl100kTokenCounter


MOCK_PROVIDER: Final = "mock"
MOCK_MODEL_NAME: Final = "mock-tool-input-extraction-v1"
PROMPT_TEMPLATE_ID: Final = "tool-input-extraction"
PROMPT_TEMPLATE_VERSION: Final = "2"
GENERATION_PARAMETERS: Final = {"temperature": 0, "max_tokens": 256}
_AGING_TEMPERATURE_PATTERN: Final = re.compile(
    r"(?:aging_temperature\s*=\s*|时效温度\s*)?730\s*°\s*C",
    re.IGNORECASE,
)


Responder = Callable[[ToolInputExtractionInput], Mapping[str, object]]


def default_mock_tool_input_responder(
    command: ToolInputExtractionInput,
) -> Mapping[str, object]:
    if _AGING_TEMPERATURE_PATTERN.fullmatch(command.content_text.strip()):
        return {
            "candidate_input_delta": {
                "aging_temperature": {"value": 730, "unit": "°C"}
            }
        }
    return {"candidate_input_delta": {}}


def _metadata(
    command: ToolInputExtractionInput,
) -> ToolInputExtractionRequestMetadata:
    messages = render_tool_input_extraction_prompt(command)
    return ToolInputExtractionRequestMetadata(
        provider=MOCK_PROVIDER,
        model_name=MOCK_MODEL_NAME,
        prompt_template_id=PROMPT_TEMPLATE_ID,
        prompt_template_version=PROMPT_TEMPLATE_VERSION,
        prompt_digest=canonical_prompt_digest(
            template_id=PROMPT_TEMPLATE_ID,
            template_version=PROMPT_TEMPLATE_VERSION,
            messages=messages,
        ),
        generation_parameters=GENERATION_PARAMETERS,
    )


class MockToolInputExtractionAdapter:
    provider = MOCK_PROVIDER
    model_name = MOCK_MODEL_NAME
    context_budget = ContextBudget(
        prompt_limit_tokens=8_192,
        history_token_budget=4_096,
        safety_margin_tokens=1_024,
        context_window_tokens=1_000_000,
        max_output_tokens=256,
    )

    def __init__(self, responder: Responder) -> None:
        if not callable(responder):
            raise TypeError("responder must be callable.")
        self._responder = responder
        self._token_counter = Cl100kTokenCounter()

    def count_prompt_tokens(self, command: ToolInputExtractionInput) -> int:
        from materialsagent.infrastructure.llm.langchain_tool_input import (
            ProviderToolInputExtractionResponse,
        )

        return self._token_counter.count_messages(
            render_tool_input_extraction_prompt(command)
        ) + self._token_counter.count_schema(
            ProviderToolInputExtractionResponse.model_json_schema()
        )

    def request_metadata(
        self,
        command: ToolInputExtractionInput,
    ) -> ToolInputExtractionRequestMetadata:
        return _metadata(command)

    def extract(
        self,
        command: ToolInputExtractionInput,
    ) -> ToolInputExtractionOutcome:
        try:
            payload = self._responder(command)
        except (ToolInputExtractionTimeoutError, ToolInputExtractionProviderError):
            raise
        except Exception:
            raise ToolInputExtractionProviderError() from None
        if not isinstance(payload, Mapping) or set(payload) != {
            "candidate_input_delta"
        }:
            raise ToolInputExtractionProtocolError()
        delta = payload["candidate_input_delta"]
        if not isinstance(delta, Mapping):
            raise ToolInputExtractionProtocolError()
        try:
            return ToolInputExtractionOutcome(
                candidate_input_delta=delta,
                request_metadata=_metadata(command),
            )
        except (TypeError, ValueError):
            raise ToolInputExtractionProtocolError() from None
