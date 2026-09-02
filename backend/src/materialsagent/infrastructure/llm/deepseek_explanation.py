from __future__ import annotations

from collections.abc import Callable, Mapping
import json
from typing import Final
import unicodedata

from langchain_deepseek import ChatDeepSeek

from materialsagent.domain.ports.explanation import (
    ExplanationInput,
    ExplanationOutcome,
    ExplanationRequestMetadata,
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


PROMPT_TEMPLATE_ID: Final = "tool-result-explanation"
PROMPT_TEMPLATE_VERSION: Final = "2"
GENERATION_PARAMETERS: Final = {
    "temperature": 0,
    "max_tokens": 768,
    "thinking_mode": "disabled",
    "response_format": "text",
    "streaming": False,
}
PUBLIC_FAILURES: Final = {
    "LLM_TIMEOUT": (
        "EXPLANATION_TIMEOUT",
        "Explanation generation timed out.",
    ),
    "LLM_AUTHENTICATION_FAILED": (
        "EXPLANATION_PROVIDER_UNAVAILABLE",
        "Explanation provider is unavailable.",
    ),
    "LLM_BALANCE_EXHAUSTED": (
        "EXPLANATION_PROVIDER_UNAVAILABLE",
        "Explanation provider is unavailable.",
    ),
    "LLM_RATE_LIMITED": (
        "EXPLANATION_PROVIDER_UNAVAILABLE",
        "Explanation provider is unavailable.",
    ),
    "LLM_PROVIDER_UNAVAILABLE": (
        "EXPLANATION_PROVIDER_UNAVAILABLE",
        "Explanation provider is unavailable.",
    ),
    "LLM_REQUEST_REJECTED": (
        "EXPLANATION_PROVIDER_UNAVAILABLE",
        "Explanation provider is unavailable.",
    ),
    "LLM_EMPTY_RESPONSE": (
        "EXPLANATION_PROTOCOL_ERROR",
        "Explanation provider returned an invalid response.",
    ),
    "LLM_SCHEMA_MISMATCH": (
        "EXPLANATION_PROTOCOL_ERROR",
        "Explanation provider returned an invalid response.",
    ),
}
UNKNOWN_PUBLIC_FAILURE: Final = (
    "EXPLANATION_FAILED",
    "Explanation generation failed.",
)


def _plain_json(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): _plain_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_plain_json(item) for item in value]
    return value


def _projection(value: ExplanationInput) -> dict[str, object]:
    return {
        "result_id": value.result_id,
        "status": value.status,
        "requested_outputs": list(value.requested_outputs),
        "completed_outputs": list(value.completed_outputs),
        "failed_outputs": list(value.failed_outputs),
        "data": _plain_json(value.data),
        "artifacts": [
            {
                "asset_id": item.asset_id,
                "role": item.role,
                "asset_type": item.asset_type,
            }
            for item in value.artifacts
        ],
        "warnings": _plain_json(value.warnings),
        "error": _plain_json(value.error),
        "process_parameters": _plain_json(value.process_parameters),
        "tool_id": value.tool_id,
        "tool_version": value.tool_version,
        "schema_hash": value.schema_hash,
    }


def _render_messages(value: ExplanationInput) -> list[dict[str, str]]:
    return [
        {
            "role": "system",
            "content": (
                "Explain only the supplied ToolResult facts in concise plain "
                "text. Do not invent values or expose internal reasoning."
            ),
        },
        {
            "role": "user",
            "content": json.dumps(
                _projection(value),
                sort_keys=True,
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
            ),
        },
    ]


def _failed_outcome(
    llm_error_code: str,
    *,
    provider_request_id: str | None = None,
    llm_safe_error_message: str | None = None,
) -> ExplanationOutcome:
    error_code, safe_error_message = PUBLIC_FAILURES.get(
        llm_error_code,
        UNKNOWN_PUBLIC_FAILURE,
    )
    return ExplanationOutcome(
        text=None,
        usage=None,
        provider_request_id=provider_request_id,
        error_code=error_code,
        safe_error_message=safe_error_message,
        llm_error_code=llm_error_code,
        llm_safe_error_message=(
            llm_safe_error_message or SAFE_MESSAGES[llm_error_code]
        ),
    )


def _normalize_text(content: str) -> str | None:
    normalized = (
        content.replace("\r\n", " ")
        .replace("\r", " ")
        .replace("\n", " ")
        .replace("\t", " ")
    )
    if any(
        unicodedata.category(character).startswith("C")
        for character in normalized
    ):
        return None
    normalized = " ".join(normalized.split())
    if (
        not normalized
        or len(normalized) > 4096
        or not normalized.isprintable()
    ):
        return None
    return normalized


class DeepSeekExplanationAdapter:
    provider = "deepseek"
    model_name = "deepseek-v4-flash"
    prompt_template_id = PROMPT_TEMPLATE_ID
    prompt_template_version = PROMPT_TEMPLATE_VERSION

    def __init__(
        self,
        config: DeepSeekConfig,
        *,
        chat_model: object | None = None,
        model_factory: Callable[..., object] | None = None,
    ) -> None:
        factory = model_factory or ChatDeepSeek
        self._chat_model = (
            chat_model
            if chat_model is not None
            else factory(
                **deepseek_client_kwargs(config, max_tokens=768)
            )
        )

    def request_metadata(
        self,
        value: ExplanationInput,
    ) -> ExplanationRequestMetadata:
        messages = _render_messages(value)
        return ExplanationRequestMetadata(
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

    def explain(self, value: ExplanationInput) -> ExplanationOutcome:
        messages = _render_messages(value)
        try:
            raw = self._chat_model.invoke(messages)  # type: ignore[attr-defined]
        except Exception as error:
            failure = classify_provider_exception(error)
            return _failed_outcome(
                failure.error_code,
                provider_request_id=failure.provider_request_id,
                llm_safe_error_message=failure.safe_error_message,
            )
        content = getattr(raw, "content", None)
        if not isinstance(content, str) or not content.strip():
            return _failed_outcome("LLM_EMPTY_RESPONSE")
        text = _normalize_text(content)
        if text is None:
            return _failed_outcome("LLM_SCHEMA_MISMATCH")
        return ExplanationOutcome(
            text=text,
            usage=controlled_usage(raw),
            provider_request_id=success_provider_request_id(raw),
            error_code=None,
            safe_error_message=None,
        )
