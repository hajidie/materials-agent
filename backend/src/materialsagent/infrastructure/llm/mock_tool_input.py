from __future__ import annotations

from collections.abc import Callable, Mapping
from hashlib import sha256
import json
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


MOCK_PROVIDER: Final = "mock"
MOCK_MODEL_NAME: Final = "mock-tool-input-extraction-v1"
PROMPT_TEMPLATE_ID: Final = "tool-input-extraction"
PROMPT_TEMPLATE_VERSION: Final = "1"
GENERATION_PARAMETERS: Final = {"temperature": 0, "max_tokens": 256}
_AGING_TEMPERATURE_PATTERN: Final = re.compile(
    r"(?:aging_temperature\s*=\s*|时效温度\s*)?730\s*°\s*C",
    re.IGNORECASE,
)


Responder = Callable[[ToolInputExtractionInput], Mapping[str, object]]


def _plain_json(value: object) -> object:
    if isinstance(value, Mapping):
        return {key: _plain_json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain_json(item) for item in value]
    return value


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
    controlled_context = {
        "tool_id": command.tool_context_ref.tool_id,
        "version": command.tool_context_ref.version,
        "schema_hash": command.tool_context_ref.schema_hash,
        "candidate_input_schema": _plain_json(command.candidate_input_schema),
        "missing_fields": list(command.missing_fields),
        "ambiguous_fields": list(command.ambiguous_fields),
        "content_text": command.content_text,
    }
    canonical = json.dumps(
        controlled_context,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return ToolInputExtractionRequestMetadata(
        provider=MOCK_PROVIDER,
        model_name=MOCK_MODEL_NAME,
        prompt_template_id=PROMPT_TEMPLATE_ID,
        prompt_template_version=PROMPT_TEMPLATE_VERSION,
        prompt_digest=sha256(canonical.encode("utf-8")).hexdigest(),
        generation_parameters=GENERATION_PARAMETERS,
    )


class MockToolInputExtractionAdapter:
    provider = MOCK_PROVIDER
    model_name = MOCK_MODEL_NAME

    def __init__(self, responder: Responder) -> None:
        if not callable(responder):
            raise TypeError("responder must be callable.")
        self._responder = responder

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
