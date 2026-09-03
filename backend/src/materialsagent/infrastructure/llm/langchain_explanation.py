from __future__ import annotations

import unicodedata

from materialsagent.domain.ports.explanation import (
    ExplanationInput,
    ExplanationOutcome,
    ExplanationRequestMetadata,
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
    TOOL_RESULT_EXPLANATION_PROMPT_ID,
    TOOL_RESULT_EXPLANATION_PROMPT_VERSION,
    render_tool_result_explanation_prompt,
)


PUBLIC_FAILURES = {
    "LLM_TIMEOUT": ("EXPLANATION_TIMEOUT", "Explanation generation timed out."),
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
UNKNOWN_PUBLIC_FAILURE = ("EXPLANATION_FAILED", "Explanation generation failed.")


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
    if not normalized or len(normalized) > 4096 or not normalized.isprintable():
        return None
    return normalized


class LangChainExplanationAdapter:
    prompt_template_id = TOOL_RESULT_EXPLANATION_PROMPT_ID
    prompt_template_version = TOOL_RESULT_EXPLANATION_PROMPT_VERSION

    def __init__(
        self,
        config: ConfiguredRole,
        *,
        chat_model: object | None = None,
    ) -> None:
        if config.role != "tool_result_explanation":
            raise ValueError(
                "Explanation adapter requires tool_result_explanation config."
            )
        self._config = config
        self._prompt_cache = PromptRenderCache()
        self.provider = config.provider
        self.model_name = config.model_name
        self._chat_model = chat_model or create_chat_model(config)

    def request_metadata(
        self,
        value: ExplanationInput,
    ) -> ExplanationRequestMetadata:
        messages = render_tool_result_explanation_prompt(value)
        self._prompt_cache.store(value, messages)
        return ExplanationRequestMetadata(
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

    def explain(self, value: ExplanationInput) -> ExplanationOutcome:
        messages = self._prompt_cache.take(value)
        if messages is None:
            messages = render_tool_result_explanation_prompt(value)
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
