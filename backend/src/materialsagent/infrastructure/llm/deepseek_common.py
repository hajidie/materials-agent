from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from hashlib import sha256
import json
from typing import Final

import httpx
import openai

from materialsagent.domain.ports.chat_orchestration import (
    PROVIDER_REQUEST_ID_PATTERN,
)
from materialsagent.infrastructure.config import DeepSeekConfig


SAFE_MESSAGES: Final = {
    "LLM_TIMEOUT": "LLM provider request timed out.",
    "LLM_AUTHENTICATION_FAILED": "LLM provider authentication failed.",
    "LLM_BALANCE_EXHAUSTED": "LLM provider balance is exhausted.",
    "LLM_RATE_LIMITED": "LLM provider rate limit was exceeded.",
    "LLM_PROVIDER_UNAVAILABLE": "LLM provider is unavailable.",
    "LLM_REQUEST_REJECTED": "LLM provider rejected the request.",
    "LLM_EMPTY_RESPONSE": "LLM provider returned an empty response.",
    "LLM_INVALID_JSON": "LLM provider returned invalid JSON.",
    "LLM_SCHEMA_MISMATCH": (
        "LLM provider response did not match the required schema."
    ),
}


@dataclass(frozen=True, slots=True)
class ControlledProviderFailure:
    error_code: str
    safe_error_message: str
    provider_request_id: str | None = None


def deepseek_client_kwargs(
    config: DeepSeekConfig,
    *,
    max_tokens: int,
) -> dict[str, object]:
    return {
        "model": config.model_name,
        "api_key": config.api_key,
        "base_url": config.base_url,
        "timeout": config.timeout_seconds,
        "max_retries": 0,
        "temperature": 0,
        "max_tokens": max_tokens,
        "streaming": False,
        "include_response_headers": True,
        "extra_body": {"thinking": {"type": "disabled"}},
    }


def canonical_prompt_digest(
    *,
    template_id: str,
    template_version: str,
    messages: Sequence[Mapping[str, str]],
) -> str:
    canonical_json = json.dumps(
        {
            "template_id": template_id,
            "template_version": template_version,
            "messages": list(messages),
        },
        sort_keys=True,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    )
    return sha256(canonical_json.encode("utf-8")).hexdigest()


def controlled_usage(raw: object) -> dict[str, int] | None:
    usage = getattr(raw, "usage_metadata", None)
    if not isinstance(usage, Mapping):
        return None
    input_tokens = usage.get("input_tokens")
    output_tokens = usage.get("output_tokens")
    if (
        type(input_tokens) is not int
        or input_tokens < 0
        or type(output_tokens) is not int
        or output_tokens < 0
    ):
        return None
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
    }


def _controlled_request_id(value: object) -> str | None:
    if (
        not isinstance(value, str)
        or PROVIDER_REQUEST_ID_PATTERN.fullmatch(value) is None
    ):
        return None
    return value


def success_provider_request_id(raw: object) -> str | None:
    response_metadata = getattr(raw, "response_metadata", None)
    if not isinstance(response_metadata, Mapping):
        return None
    headers = response_metadata.get("headers")
    if not isinstance(headers, Mapping):
        return None
    for key, value in headers.items():
        if isinstance(key, str) and key.casefold() == "x-request-id":
            return _controlled_request_id(value)
    return None


def failure_provider_request_id(error: BaseException) -> str | None:
    return _controlled_request_id(getattr(error, "request_id", None))


def static_failure(
    error_code: str,
    *,
    provider_request_id: str | None = None,
) -> ControlledProviderFailure:
    return ControlledProviderFailure(
        error_code=error_code,
        safe_error_message=SAFE_MESSAGES[error_code],
        provider_request_id=provider_request_id,
    )


def classify_provider_exception(
    error: BaseException,
) -> ControlledProviderFailure:
    request_id = failure_provider_request_id(error)
    if isinstance(error, (openai.APITimeoutError, httpx.TimeoutException)):
        return static_failure("LLM_TIMEOUT", provider_request_id=request_id)
    if isinstance(error, (openai.APIConnectionError, ConnectionError)):
        return static_failure(
            "LLM_PROVIDER_UNAVAILABLE",
            provider_request_id=request_id,
        )

    status_code = getattr(error, "status_code", None)
    error_code = {
        400: "LLM_REQUEST_REJECTED",
        401: "LLM_AUTHENTICATION_FAILED",
        402: "LLM_BALANCE_EXHAUSTED",
        422: "LLM_REQUEST_REJECTED",
        429: "LLM_RATE_LIMITED",
        500: "LLM_PROVIDER_UNAVAILABLE",
        503: "LLM_PROVIDER_UNAVAILABLE",
    }.get(status_code, "LLM_PROVIDER_UNAVAILABLE")
    return static_failure(error_code, provider_request_id=request_id)
