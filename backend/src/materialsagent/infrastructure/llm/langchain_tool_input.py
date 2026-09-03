from __future__ import annotations

from collections.abc import Mapping
import json
from typing import Any

from langchain_core.exceptions import OutputParserException
from pydantic import (
    BaseModel,
    ConfigDict,
    JsonValue,
    StrictStr,
    ValidationError,
    model_validator,
)

from materialsagent.domain.ports.tool_input_extraction import (
    ToolInputExtractionInput,
    ToolInputExtractionOutcome,
    ToolInputExtractionProtocolError,
    ToolInputExtractionProviderError,
    ToolInputExtractionRequestMetadata,
    ToolInputExtractionTimeoutError,
)
from materialsagent.infrastructure.llm.common import (
    PromptRenderCache,
    SAFE_MESSAGES,
    canonical_prompt_digest,
    classify_provider_exception,
    controlled_usage,
    success_provider_request_id,
)
from materialsagent.infrastructure.llm.configuration import ConfiguredRole
from materialsagent.infrastructure.llm.factory import create_chat_model
from materialsagent.infrastructure.llm.prompts import (
    TOOL_INPUT_EXTRACTION_PROMPT_ID,
    TOOL_INPUT_EXTRACTION_PROMPT_VERSION,
    render_tool_input_extraction_prompt,
)


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


class LangChainToolInputExtractionAdapter:
    prompt_template_id = TOOL_INPUT_EXTRACTION_PROMPT_ID
    prompt_template_version = TOOL_INPUT_EXTRACTION_PROMPT_VERSION

    def __init__(
        self,
        config: ConfiguredRole,
        *,
        chat_model: object | None = None,
        structured_runnable: object | None = None,
    ) -> None:
        if config.role != "tool_input_extraction":
            raise ValueError(
                "Tool input adapter requires tool_input_extraction config."
            )
        self._config = config
        self._prompt_cache = PromptRenderCache()
        self.provider = config.provider
        self.model_name = config.model_name
        if structured_runnable is None:
            model = chat_model or create_chat_model(config)
            structured_runnable = model.with_structured_output(  # type: ignore[attr-defined]
                ProviderToolInputExtractionResponse,
                method="json_mode",
                include_raw=True,
            )
        self._structured_runnable = structured_runnable

    def request_metadata(
        self,
        command: ToolInputExtractionInput,
    ) -> ToolInputExtractionRequestMetadata:
        messages = render_tool_input_extraction_prompt(command)
        self._prompt_cache.store(command, messages)
        return self._metadata_from_messages(messages)

    def _metadata_from_messages(
        self,
        messages: list[dict[str, str]],
    ) -> ToolInputExtractionRequestMetadata:
        return ToolInputExtractionRequestMetadata(
            provider=self.provider,
            model_name=self.model_name,
            prompt_template_id=self.prompt_template_id,
            prompt_template_version=self.prompt_template_version,
            prompt_digest=canonical_prompt_digest(
                template_id=self.prompt_template_id,
                template_version=self.prompt_template_version,
                messages=messages,
            ),
            generation_parameters=self._config.generation_parameters,
        )

    def extract(
        self,
        command: ToolInputExtractionInput,
    ) -> ToolInputExtractionOutcome:
        messages = self._prompt_cache.take(command)
        if messages is None:
            messages = render_tool_input_extraction_prompt(command)
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
                request_metadata=self._metadata_from_messages(messages),
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
