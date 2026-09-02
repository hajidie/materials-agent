from __future__ import annotations

from collections.abc import Callable, Mapping
import json
from typing import Any, Final

from langchain_core.exceptions import OutputParserException
from langchain_deepseek import ChatDeepSeek
from pydantic import BaseModel, ConfigDict, JsonValue, StrictStr, ValidationError, model_validator

from materialsagent.domain.ports.tool_input_extraction import (
    ToolInputExtractionInput,
    ToolInputExtractionOutcome,
    ToolInputExtractionProtocolError,
    ToolInputExtractionProviderError,
    ToolInputExtractionRequestMetadata,
    ToolInputExtractionTimeoutError,
)
from materialsagent.infrastructure.config import DeepSeekConfig
from materialsagent.infrastructure.llm.deepseek_common import (
    SAFE_MESSAGES,
    canonical_prompt_digest,
    classify_provider_exception,
    controlled_usage,
    deepseek_client_kwargs,
    success_provider_request_id,
)


PROMPT_TEMPLATE_ID: Final = "tool-input-extraction"
PROMPT_TEMPLATE_VERSION: Final = "1"
GENERATION_PARAMETERS: Final = {
    "temperature": 0,
    "max_tokens": 1024,
    "thinking_mode": "disabled",
    "response_format": "json_object",
    "streaming": False,
}


class ProviderToolInputExtractionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    candidate_input_delta: dict[StrictStr, JsonValue]

    @model_validator(mode="after")
    def require_bounded_delta(self) -> ProviderToolInputExtractionResponse:
        encoded = json.dumps(
            self.candidate_input_delta,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        if len(encoded) > 4096:
            raise ValueError("candidate_input_delta exceeds the safe size limit.")
        return self


def _plain_json(value: object) -> object:
    if isinstance(value, Mapping):
        return {key: _plain_json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain_json(item) for item in value]
    return value


def _render_messages(value: ToolInputExtractionInput) -> list[dict[str, str]]:
    context = json.dumps(
        {
            "tool_context_ref": {
                "tool_id": value.tool_context_ref.tool_id,
                "version": value.tool_context_ref.version,
                "schema_hash": value.tool_context_ref.schema_hash,
            },
            "candidate_input_schema": _plain_json(
                value.candidate_input_schema
            ),
            "missing_fields": list(value.missing_fields),
            "ambiguous_fields": list(value.ambiguous_fields),
        },
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return [
        {
            "role": "system",
            "content": (
                "Extract only newly supplied values for this already-bound Tool. "
                "Use only the supplied Tool context and candidate input schema. "
                "Return exactly one JSON object shaped as "
                "{\"candidate_input_delta\":{}} and no other text. Do not return "
                "a Tool ID, route, task status, execution permission, version, or "
                f"schema hash. Bound Tool context: {context}"
            ),
        },
        {"role": "user", "content": value.content_text},
    ]


def _metadata(value: ToolInputExtractionInput) -> ToolInputExtractionRequestMetadata:
    messages = _render_messages(value)
    return ToolInputExtractionRequestMetadata(
        provider="deepseek",
        model_name="deepseek-v4-flash",
        prompt_template_id=PROMPT_TEMPLATE_ID,
        prompt_template_version=PROMPT_TEMPLATE_VERSION,
        prompt_digest=canonical_prompt_digest(
            template_id=PROMPT_TEMPLATE_ID,
            template_version=PROMPT_TEMPLATE_VERSION,
            messages=messages,
        ),
        generation_parameters=GENERATION_PARAMETERS,
    )


class DeepSeekToolInputExtractionAdapter:
    provider = "deepseek"
    model_name = "deepseek-v4-flash"

    def __init__(
        self,
        config: DeepSeekConfig,
        *,
        structured_runnable: object | None = None,
        model_factory: Callable[..., object] | None = None,
    ) -> None:
        if structured_runnable is None:
            factory = model_factory or ChatDeepSeek
            model = factory(
                **deepseek_client_kwargs(config, max_tokens=1024)
            )
            structured_runnable = model.with_structured_output(
                ProviderToolInputExtractionResponse,
                method="json_mode",
                include_raw=True,
            )
        self._structured_runnable = structured_runnable

    def extract(
        self,
        command: ToolInputExtractionInput,
    ) -> ToolInputExtractionOutcome:
        messages = _render_messages(command)
        try:
            envelope = self._structured_runnable.invoke(messages)  # type: ignore[attr-defined]
        except json.JSONDecodeError:
            self._raise_protocol("LLM_INVALID_JSON")
        except (OutputParserException, ValidationError):
            self._raise_protocol("LLM_SCHEMA_MISMATCH")
        except Exception as error:
            failure = classify_provider_exception(error)
            error_type = (
                ToolInputExtractionTimeoutError
                if failure.error_code == "LLM_TIMEOUT"
                else ToolInputExtractionProviderError
            )
            raise error_type(
                error_code=failure.error_code,
                safe_error_message=failure.safe_error_message,
                provider_request_id=failure.provider_request_id,
            ) from None
        if not isinstance(envelope, Mapping):
            self._raise_protocol("LLM_SCHEMA_MISMATCH")
        raw = envelope.get("raw")
        content = getattr(raw, "content", None)
        if raw is None or not isinstance(content, str) or not content.strip():
            self._raise_protocol("LLM_EMPTY_RESPONSE")
        if envelope.get("parsing_error") is not None:
            try:
                json.loads(content)
            except json.JSONDecodeError:
                self._raise_protocol("LLM_INVALID_JSON")
            self._raise_protocol("LLM_SCHEMA_MISMATCH")
        try:
            parsed = ProviderToolInputExtractionResponse.model_validate(
                envelope.get("parsed")
            )
            return ToolInputExtractionOutcome(
                candidate_input_delta=parsed.candidate_input_delta,
                request_metadata=_metadata(command),
                usage=controlled_usage(raw),
                provider_request_id=success_provider_request_id(raw),
            )
        except (ValidationError, TypeError, ValueError):
            self._raise_protocol("LLM_SCHEMA_MISMATCH")

    @staticmethod
    def _raise_protocol(error_code: str) -> Any:
        raise ToolInputExtractionProtocolError(
            error_code=error_code,
            safe_error_message=SAFE_MESSAGES[error_code],
        )
